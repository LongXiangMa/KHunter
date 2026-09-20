"""信号层：复用 KHunter 原始策略代码生成历史信号矩阵。

设计取舍
--------
重新用向量化代码"转写" 19 个策略的条件，速度最快但保真风险高（条件多达数百个，
且部分策略含状态机）。本模块改用另一条路：

    1. 指标只算一次 —— 策略的 ``calculate_indicators`` 在整段历史上跑一遍；
    2. 逐日只做"切片 + 判断" —— 把 ``calculate_indicators`` 临时替换为恒等函数，
       让原始 ``select_stocks`` 直接吃预计算好的指标切片。

由于滚动指标是因果的（第 i 行的值只依赖 <= i 的数据），"整段算一次再切片"
与"每次截断重算"结果相同。该假设由 ``vector_bt.validate`` 抽样校验。

实测：单次评估 0.23 ms → 约 0.09 ms；结合数据层优化后，
19 个策略 × 1 年区间的信号生成从数小时降到分钟级。
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd
import yaml

from vector_bt.paths import ROOT

DEFAULT_KHUNTER_ROOT = ROOT

_catalog_cache: dict[Path, dict[str, tuple[str, str]]] = {}


def _camel_to_snake(name: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def ensure_khunter_importable(khunter_root: Path | str | None = None) -> Path:
    """把 KHunter 根目录加入 sys.path（策略模块内部按 ``strategy.xxx`` 相互引用）。"""
    # 注意：显式传 None 也要回落到默认路径（CLI 未指定时就是 None）
    root = Path(khunter_root or DEFAULT_KHUNTER_ROOT).resolve()
    if not (root / "strategy").is_dir():
        raise FileNotFoundError(f"{root} 下找不到 strategy 目录，KHunter 路径是否正确？")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def strategy_catalog(khunter_root: Path | str | None = None) -> dict[str, tuple[str, str]]:
    """扫描 ``strategy/`` 目录，返回 {中文名: (模块名, 类名)}。

    中文名取自 ``config/strategy_name_mapping.yaml``；类名按基类 ``BaseStrategy``
    识别，避免把工具类误当成策略。
    """
    root = ensure_khunter_importable(khunter_root)
    if root in _catalog_cache:
        return _catalog_cache[root]

    from strategy.base_strategy import BaseStrategy  # noqa: WPS433 (延迟导入)

    mapping_file = root / "config" / "strategy_name_mapping.yaml"
    class_to_cn: dict[str, str] = {}
    if mapping_file.exists():
        with open(mapping_file, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        class_to_cn = dict(cfg.get("strategy_names", {}) or {})

    catalog: dict[str, tuple[str, str]] = {}
    for path in sorted((root / "strategy").glob("*.py")):
        if path.name.startswith("_") or path.stem in {
            "base_strategy",
            "strategy_registry",
            "pattern_library",
            "pattern_config",
            "pattern_matcher",
            "pattern_feature_extractor",
            "param_lock",
            "param_tracker",
            "parallel_strategy_executor",
        }:
            continue
        module_name = f"strategy.{path.stem}"
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        for attr in dir(module):
            obj = getattr(module, attr)
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseStrategy)
                and obj is not BaseStrategy
                and obj.__module__ == module.__name__
            ):
                display = class_to_cn.get(attr, getattr(obj, "name", attr) or attr)
                catalog.setdefault(str(display), (module_name, attr))
                catalog.setdefault(attr, (module_name, attr))
    _catalog_cache[root] = catalog
    return catalog


def load_strategy_params(
    class_name: str, khunter_root: Path | str | None = None
) -> dict[str, Any]:
    """读取 ``config/strategy_params.yaml`` 中该策略的默认参数（与 Web 端一致）。"""
    root = Path(khunter_root or DEFAULT_KHUNTER_ROOT)
    params_file = root / "config" / "strategy_params.yaml"
    if not params_file.exists():
        return {}
    with open(params_file, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    node = (cfg.get("strategies") or {}).get(class_name) or {}
    params = dict(node.get("params") or {})
    # 与 StrategyRegistry._convert_param_types 保持一致：
    # YAML 里 ma_periods 常写成 "5,10,20" 这样的字符串，不转换会导致 rolling(window='5') 报错。
    if "ma_periods" in params and params["ma_periods"] is not None:
        params["ma_periods"] = _parse_ma_periods(params["ma_periods"])
    if params.get("volume_ratio_max") == "null":
        params["volume_ratio_max"] = None
    return params


def _parse_ma_periods(value: Any) -> list[int]:
    """把 "5,10,20" / "[5, 10, 20]" / [5,10,20] 统一成 int 列表。"""
    if isinstance(value, list):
        return [int(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return [int(part) for part in (p.strip() for p in text.split(",")) if part]
    return [int(value)]


@dataclass
class SignalMatrix:
    """一只股票在某策略下的信号。"""

    strategy: str
    class_name: str
    by_code: dict[str, dict[str, float]] = field(default_factory=dict)
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)

    def to_frame(self, columns: Iterable[str] | None = None) -> pd.DataFrame:
        """转成宽表：index=日期，columns=股票代码，value=信号分数（无信号为 NaN）。"""
        records = [
            {"date": date, "code": code, "score": score}
            for code, series in self.by_code.items()
            for date, score in series.items()
        ]
        if not records:
            cols = list(columns or [])
            return pd.DataFrame(columns=cols)
        frame = pd.DataFrame.from_records(records)
        wide = frame.pivot_table(
            index="date", columns="code", values="score", aggfunc="max"
        )
        return wide.sort_index()


class StrategyAdapter:
    """把 KHunter 策略包装成"整段历史一次评估"的适配器。"""

    def __init__(
        self,
        name: str,
        *,
        khunter_root: Path | str | None = None,
        params: Mapping[str, Any] | None = None,
        window: int = 180,
        min_history: int = 60,
    ) -> None:
        self.root = ensure_khunter_importable(khunter_root)
        catalog = strategy_catalog(self.root)
        if name not in catalog:
            raise KeyError(
                f"未找到策略 {name!r}；可用：{sorted({k for k in catalog if not k.isascii()})}"
            )
        module_name, self.class_name = catalog[name]
        module = importlib.import_module(module_name)
        self.name = name

        merged_params = load_strategy_params(self.class_name, self.root)
        if params:
            merged_params.update(params)
        self.strategy = getattr(module, self.class_name)(params=merged_params or None)

        self.window = int(window)
        self.min_history = int(min_history)
        self._orig_calculate_indicators = self.strategy.calculate_indicators
        # 关键优化：让内部再次调用 calculate_indicators 时直接返回传入的切片
        self.strategy.calculate_indicators = lambda df: df
        # 统计扫描过程中的策略异常，供报告使用（不静默吞掉）
        self.errors = 0
        self.error_samples: list[str] = []

        # 缓存 select_stocks 的可选参数（原实现每次调用都做一次 inspect，代价不低）
        try:
            sig = inspect.signature(self.strategy.select_stocks).parameters
        except (TypeError, ValueError):
            sig = {}
        self._select_kwargs = [k for k in ("selection_date", "stock_code") if k in sig]

    # -- 指标 --------------------------------------------------------------
    def prepare(self, df_input: pd.DataFrame) -> pd.DataFrame:
        """在整段历史上计算一次指标。

        内部会先把数据规范成**降序（最新在前）**——这是 KHunter 真实管线的朝向
        （``read_all_stocks_kline`` 升序取数，web_server 再排成降序后交给策略）。
        朝向之所以重要，是因为不同策略对输入的假设不同：

        * 2560战法：内部把数据排序成升序，输出升序；
        * 仙人指路：假定输入为降序，自行反转计算后再翻回，输出降序。

        因此不能由本工具规定朝向，只能沿用策略自身输出，后续切片保持同一朝向。
        """
        frame = df_input
        if len(frame) > 1 and str(frame["date"].iloc[0]) < str(frame["date"].iloc[-1]):
            frame = frame.iloc[::-1]
        return self._orig_calculate_indicators(frame.reset_index(drop=True).copy())

    # -- 逐日评估 ----------------------------------------------------------
    def scan(
        self,
        df_indicators: pd.DataFrame,
        code: str,
        stock_name: str = "",
        only_dates: Iterable[str] | None = None,
        raw_frame: pd.DataFrame | None = None,
    ) -> dict[str, float]:
        """返回 {日期: 信号分数}。df_indicators 必须是 prepare() 的产物。

        与 KHunter 原流程的对应关系（非常重要）：
          * 原流程把 ``calculate_indicators`` 的**输出**交给 ``select_stocks``，
            所以这里切片的是输出帧本身，朝向由策略决定（升序或降序都支持）；
          * ``quick_filter`` 在原流程中拿到的是**原始输入**（降序、未算指标），
            所以这里用 raw_frame 对应的降序窗口单独调用它。

        only_dates：只评估这些日期（例如按周调仓只关心周五）；预热期仍用完整历史。
        """
        n = len(df_indicators)
        if n < self.min_history:
            return {}

        dates = [str(value)[:10] for value in df_indicators["date"].tolist()]
        ascending = dates[0] <= dates[-1]
        wanted = set(only_dates) if only_dates is not None else None
        raw = raw_frame if raw_frame is not None else df_indicators
        raw_dates = [str(value)[:10] for value in raw["date"].tolist()]
        if raw_dates and raw_dates[0] < raw_dates[-1]:
            raw = raw.iloc[::-1]
            raw_dates = raw_dates[::-1]
        raw_pos = {date: i for i, date in enumerate(raw_dates)}

        out: dict[str, float] = {}
        for idx in range(n):
            current = dates[idx]
            if wanted is not None and current not in wanted:
                continue
            # 指标帧既可能是升序也可能是降序，窗口边界按朝向分别取
            if ascending:
                lo, hi = max(0, idx - self.window + 1), idx + 1
            else:
                lo, hi = idx, min(n, idx + self.window)
            if hi - lo < self.min_history:
                continue
            window_ind = df_indicators.iloc[lo:hi]
            edge_newest = dates[lo] if not ascending else dates[hi - 1]
            edge_oldest = dates[hi - 1] if not ascending else dates[lo]
            try:
                # 1) 快速过滤：与真实流程一致，喂降序原始数据
                raw_lo = raw_pos.get(edge_newest, 0)
                raw_hi = raw_pos.get(edge_oldest, len(raw_dates) - 1)
                raw_window_desc = raw.iloc[raw_lo : raw_hi + 1]
                if hasattr(self.strategy, "_quick_filter_with_lookback"):
                    passed = self.strategy._quick_filter_with_lookback(raw_window_desc)
                else:
                    passed = self.strategy.quick_filter(raw_window_desc)
                if not passed:
                    continue

                # 2) 选股：喂 calculate_indicators 的输出去切片（朝向由策略决定）
                kwargs = {}
                if "selection_date" in self._select_kwargs:
                    kwargs["selection_date"] = current
                if "stock_code" in self._select_kwargs:
                    kwargs["stock_code"] = code
                signals = self.strategy.select_stocks(window_ind, stock_name, **kwargs)
            except Exception as exc:  # 策略内部异常不应中断整轮回测
                self.errors += 1
                if len(self.error_samples) < 5:
                    self.error_samples.append(f"{code}@{current}: {exc}")
                continue
            if signals:
                score = signals[0].get("strategy_weight", 1.0)
                try:
                    score = float(score)
                except (TypeError, ValueError):
                    score = 1.0
                out[current] = score
        return out

    # -- 原始（未优化）路径，供保真校验使用 ---------------------------------
    def scan_reference(
        self, df_asc: pd.DataFrame, code: str, stock_name: str = "", dates: Iterable[str] | None = None
    ) -> dict[str, float]:
        """不做任何优化的参考实现：每个日期都截断数据、重算指标。"""
        out: dict[str, float] = {}
        date_series = [str(v)[:10] for v in df_asc["date"].tolist()]
        targets = set(dates) if dates is not None else set(date_series)
        for idx, date in enumerate(date_series):
            if date not in targets or idx + 1 < self.min_history:
                continue
            truncated = df_asc.iloc[: idx + 1]
            signals = self._orig_execute(truncated, code, stock_name, date)
            if signals:
                score = signals[0].get("strategy_weight", 1.0)
                try:
                    score = float(score)
                except (TypeError, ValueError):
                    score = 1.0
                out[date] = score
        return out

    def _orig_execute(
        self, df_asc: pd.DataFrame, code: str, stock_name: str, date: str
    ) -> list[dict]:
        """用原始 calculate_indicators 走一遍 execute_selection。"""
        saved = self.strategy.calculate_indicators
        self.strategy.calculate_indicators = self._orig_calculate_indicators
        try:
            desc = df_asc.iloc[::-1].reset_index(drop=True)
            return self.strategy.execute_selection(desc, code, stock_name, selection_date=date)
        finally:
            self.strategy.calculate_indicators = saved


def available_strategies(khunter_root: Path | str | None = None) -> list[str]:
    """返回可用的中文策略名列表（过滤掉类别名条目）。"""
    catalog = strategy_catalog(khunter_root)
    return sorted({k for k in catalog if any("\u4e00" <= ch <= "\u9fff" for ch in k)})
