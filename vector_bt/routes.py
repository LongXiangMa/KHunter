"""向量化回测 / 市场状态开关的 HTTP 接口（KHunter Web 端使用）。

挂载点：``/api/vector``（在 web_server.py 里注册蓝图）。

设计要点
--------
* **同步接口**：`status` / `regime` / `strategies` / `backtest`。
  这些接口命中信号缓存时是秒级；缓存未命中会现场生成（可能几十分钟），
  因此返回体里带 `cached` 字段，并在文档/界面提示。
* **异步接口**：`sweep` / `validate` 耗时较长，采用"提交任务 → 轮询进度"模式，
  与 KHunter 现有数据初始化流程的交互方式一致。
"""

from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime

import pandas as pd
from flask import Blueprint, jsonify, request

from vector_bt.benchmark import DEFAULT_BENCHMARK
from vector_bt.engine import BacktestConfig
from vector_bt.paths import CACHE_DIR
from vector_bt.regime import build_regime_filter, daily_regime_status
from vector_bt.runner import compare, sweep
from vector_bt.signals import available_strategies

vector_bp = Blueprint("vector_bt", __name__)

# 异步任务表：{task_id: {"status":..., "progress":..., "result":..., "error":...}}
_TASKS: dict[str, dict] = {}
_TASK_LOCK = threading.Lock()


def _update_task(task_id: str, **fields) -> None:
    with _TASK_LOCK:
        if task_id in _TASKS:
            _TASKS[task_id].update(fields)


def _table_to_records(table: pd.DataFrame) -> list[dict]:
    if table is None or table.empty:
        return []
    return table.replace({float("inf"): None}).where(pd.notna(table), None).to_dict("records")


@vector_bp.route("/strategies", methods=["GET"])
def list_strategies():
    """可选策略清单（含中文名）。"""
    from vector_bt.timing import TIMING_NAMES, available_timing_strategies

    return jsonify(
        {
            "success": True,
            "data": {
                "strategies": available_strategies(),
                "timing_strategies": [
                    {"id": k, "name": TIMING_NAMES.get(k, k)}
                    for k in available_timing_strategies()
                ],
            },
        }
    )


@vector_bp.route("/regime", methods=["GET"])
def regime_status():
    """最新交易日的市场活跃度状态与开仓建议。"""
    try:
        end = request.args.get("end") or None
        if end is None:
            from vector_bt.data import load_calendar

            end = load_calendar()[-1]
        window = int(request.args.get("window", 250))
        status = daily_regime_status(end=end, window=window, benchmark=DEFAULT_BENCHMARK)
        status["advice"] = (
            "高活跃，允许开新仓（建议启用涨停横盘策略）"
            if status["active"]
            else "低活跃，建议空仓或暂停开新仓"
        )
        return jsonify({"success": True, "data": status})
    except Exception as exc:
        return jsonify({"success": False, "error": f"{exc}"}), 500


@vector_bp.route("/backtest", methods=["POST"])
def run_backtest_api():
    """向量化回测：多策略对比 + 可选状态过滤。"""
    try:
        payload = request.get_json() or {}
        strategies = payload.get("strategies") or []
        start = payload.get("start")
        end = payload.get("end")
        if not strategies or not start or not end:
            return jsonify({"success": False, "error": "缺少参数：strategies / start / end"}), 400

        cfg = BacktestConfig(
            initial_capital=float(payload.get("initial_capital", 300_000)),
            hold_period=int(payload.get("hold_period", 10)),
            max_daily_buys=int(payload.get("max_daily_buys", 8)),
            stop_loss=float(payload.get("stop_loss", -0.07)),
            take_profit=float(payload.get("take_profit", 0.21)),
        )
        regime_mask = None
        method = payload.get("regime_filter", "off")
        if method != "off":
            regime_mask, _ = build_regime_filter(
                None,
                market_start=payload.get("signal_start") or start,
                market_end=end,
                method=method,
                fixed_threshold=payload.get("regime_fixed"),
            )

        unknown = [s for s in strategies if s not in available_strategies()]
        if unknown:
            return jsonify({"success": False, "error": f"未知策略: {unknown}"}), 400

        table, results, signals = compare(
            strategies,
            start=start,
            end=end,
            signal_start=payload.get("signal_start"),
            signal_end=payload.get("signal_end"),
            db_path=None,
            jobs=int(payload.get("jobs", 1)),
            window=int(payload.get("window", 180)),
            min_history=int(payload.get("min_history", 60)),
            config=cfg,
            benchmark=payload.get("benchmark", DEFAULT_BENCHMARK)
            if payload.get("benchmark", DEFAULT_BENCHMARK) != "off"
            else None,
            regime_filter=regime_mask,
            timing=payload.get("timing") or None,
            save=bool(payload.get("save", False)),
            backtest_name=payload.get("backtest_name"),
            use_cache=bool(payload.get("use_cache", True)),
            verbose=False,
        )

        curves = {name: r.equity_curve for name, r in results.items() if not r.equity_curve.empty}
        payload_curves = {
            name: {
                "dates": [d.strftime("%Y-%m-%d") for d in curve.index],
                "values": [round(float(v), 2) for v in curve.values],
            }
            for name, curve in curves.items()
        }
        return jsonify(
            {
                "success": True,
                "data": {
                    "ranking": _table_to_records(table),
                    "curves": payload_curves,
                    "signals": {k: int(v.notna().sum().sum()) for k, v in signals.items()},
                    "cached": True,
                },
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "error": f"{exc}", "trace": traceback.format_exc()[-800:]}), 500


def _run_async(task_id: str, fn, *args, **kwargs) -> None:
    """在后台线程里执行 fn，把结果写进任务表。"""

    def _target():
        try:
            _update_task(task_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))
            result = fn(*args, **kwargs)
            _update_task(task_id, status="completed", result=result)
        except Exception as exc:
            _update_task(task_id, status="failed", error=f"{exc}",
                         trace=traceback.format_exc()[-800:])

    threading.Thread(target=_target, daemon=True).start()


@vector_bp.route("/sweep", methods=["POST"])
def run_sweep_api():
    """参数扫描（异步）：固定信号，扫描止损 × 止盈 × 持有期。"""
    try:
        payload = request.get_json() or {}
        strategies = payload.get("strategies") or []
        start, end = payload.get("start"), payload.get("end")
        if not strategies or not start or not end:
            return jsonify({"success": False, "error": "缺少参数：strategies / start / end"}), 400

        task_id = uuid.uuid4().hex[:12]
        with _TASK_LOCK:
            _TASKS[task_id] = {"status": "pending", "type": "sweep", "created_at": datetime.now().isoformat(timespec="seconds")}

        def _job():
            table = sweep(
                strategies,
                start=start,
                end=end,
                stop_losses=[v / 100 for v in payload.get("stop_loss", [-5, -7, -10, -15])],
                take_profits=[v / 100 for v in payload.get("take_profit", [10, 15, 21, 30])],
                hold_periods=list(payload.get("hold_period", [5, 10, 20])),
                jobs=1,
                window=int(payload.get("window", 180)),
                benchmark=payload.get("benchmark", DEFAULT_BENCHMARK),
                use_cache=True,
                verbose=False,
            )
            return {"rows": _table_to_records(table)}

        _run_async(task_id, _job)
        return jsonify({"success": True, "data": {"task_id": task_id}})
    except Exception as exc:
        return jsonify({"success": False, "error": f"{exc}"}), 500


@vector_bp.route("/task/<task_id>", methods=["GET"])
def task_status(task_id: str):
    """查询异步任务状态。"""
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        snapshot = dict(task)
    snapshot.pop("trace", None)
    return jsonify({"success": True, "data": snapshot})


@vector_bp.route("/combo", methods=["POST"])
def run_combo_api():
    """组合扫描（异步）：选股策略 × 择时策略 矩阵。"""
    try:
        payload = request.get_json() or {}
        strategies = payload.get("strategies") or []
        timings = payload.get("timings") or [None]
        start, end = payload.get("start"), payload.get("end")
        if not strategies or not start or not end:
            return jsonify({"success": False, "error": "缺少参数：strategies / start / end"}), 400

        task_id = uuid.uuid4().hex[:12]
        with _TASK_LOCK:
            _TASKS[task_id] = {"status": "pending", "type": "combo",
                               "created_at": datetime.now().isoformat(timespec="seconds")}

        def _job():
            from vector_bt.runner import combo_sweep

            regime_mask = None
            method = payload.get("regime_filter", "off")
            if method != "off":
                regime_mask, _ = build_regime_filter(
                    None, market_start=payload.get("signal_start") or start, market_end=end,
                    method=method, fixed_threshold=payload.get("regime_fixed"),
                )
            cfg = BacktestConfig(
                initial_capital=float(payload.get("initial_capital", 300_000)),
                hold_period=int(payload.get("hold_period", 10)),
                max_daily_buys=int(payload.get("max_daily_buys", 8)),
                stop_loss=float(payload.get("stop_loss", -0.07)),
                take_profit=float(payload.get("take_profit", 0.21)),
            )
            table = combo_sweep(
                strategies,
                [t or None for t in timings],
                start=start,
                end=end,
                signal_start=payload.get("signal_start"),
                signal_end=payload.get("signal_end"),
                jobs=1,
                window=int(payload.get("window", 180)),
                min_history=int(payload.get("min_history", 60)),
                config=cfg,
                benchmark=payload.get("benchmark", DEFAULT_BENCHMARK),
                regime_filter=regime_mask,
                use_cache=True,
                verbose=False,
            )
            return {"rows": _table_to_records(table)}

        _run_async(task_id, _job)
        return jsonify({"success": True, "data": {"task_id": task_id}})
    except Exception as exc:
        return jsonify({"success": False, "error": f"{exc}"}), 500
