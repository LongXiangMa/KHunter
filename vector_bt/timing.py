"""择时策略适配器：把 KHunter 的 6 个择时策略向量化，与选股策略结合。

背景
----
KHunter 的设计是「选股 + 择时」两层：

* 选股策略（strategy/）决定**买什么**；
* 择时策略（trading/timing_strategies.py）决定**何时买/何时卖**。

而 vector_bt 早期只用了选股策略，退场靠固定的持有期 + 止损止盈，**没有结合择时**。
本模块补上这一层。

结合方式
--------
1. **进场过滤**：选股给出候选，只有择时也给出买点时才建仓；
2. **提前离场**：持仓期间若择时给出卖点，当日收盘提前平仓（在止损/止盈之外）。

评估口径与真实管线一致
----------------------
回测引擎调用择时策略时传的是**降序**数据（`backtest_engine.py:546` 附近会做一次
升序→降序的规范化），并使用 `use_prev_day_signal=True`（用倒数第二根 K 线判断
前一日是否触发），因此本模块也按同样口径评估；`position=None` 表示按"无持仓"判断买点。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd

from vector_bt.paths import ROOT

# 择时策略 ID → 中文名（与 utils/strategy_name_mapper.get_chinese_timing_name 一致）
TIMING_NAMES = {
    "turtle": "海龟策略",
    "low_turtle": "低位海龟策略",
    "support": "支撑位策略",
    "rsi": "RSI策略",
    "bollinger": "布林带策略",
    "macd_bollinger": "顺势宝策略",
}


def available_timing_strategies() -> list[str]:
    """返回可用的择时策略 ID（不含需要授权文件的顺势宝）。"""
    return [k for k in TIMING_NAMES if k != "macd_bollinger"]


def _ensure_importable() -> Path:
    root = ROOT
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


class TimingAdapter:
    """包装 KHunter 择时策略：指标只算一次，逐日切片评估买卖点。"""

    def __init__(
        self,
        name: str = "turtle",
        *,
        config: dict | None = None,
        window: int = 180,
        min_history: int = 60,
    ) -> None:
        _ensure_importable()
        if name not in TIMING_NAMES:
            raise KeyError(f"未知择时策略 {name!r}；可用：{available_timing_strategies()}")
        from trading.timing_strategies import TimingStrategyFactory

        self.name = name
        self.display_name = TIMING_NAMES[name]
        self.strategy = TimingStrategyFactory.create_strategy(name, config or {})
        self.window = int(window)
        self.min_history = int(min_history)
        self._orig_calc = self.strategy.calculate_indicators
        # 与选股适配器同样的优化：指标已在整段历史上算好，内部再调用时直接返回切片。
        # 注意必须接受 **kwargs —— 部分择时策略会以 calculate_indicators(df, stock_code=...) 调用，
        # 只收一个参数的 lambda 会抛 TypeError 并被静默吞掉，表现为"全区间 0 信号"。
        self.strategy.calculate_indicators = lambda df, *args, **kwargs: df
        self.errors = 0
        self.error_samples: list[str] = []

    def prepare(self, df_input: pd.DataFrame) -> pd.DataFrame:
        """在整段历史上计算一次指标（输入先规范成降序，与真实管线一致）。"""
        frame = df_input
        if len(frame) > 1 and str(frame["date"].iloc[0]) < str(frame["date"].iloc[-1]):
            frame = frame.iloc[::-1]
        return self._orig_calc(frame.reset_index(drop=True).copy())

    def scan(
        self,
        df_indicators: pd.DataFrame,
        code: str,
        only_dates: Iterable[str] | None = None,
    ) -> dict[str, dict[str, bool]]:
        """返回 {日期: {"buy": bool, "sell": bool}}。

        position 传 None，因此这里得到的是"空仓视角"的买点/卖点 ——
        买点用于进场过滤；卖点用于持仓期间的提前离场（近似：
        不区分持仓天数与加仓次数，只按策略自身的离场条件判断）。
        """
        n = len(df_indicators)
        if n < self.min_history:
            return {}
        dates = [str(v)[:10] for v in df_indicators["date"].tolist()]
        ascending = dates[0] <= dates[-1]
        wanted = set(only_dates) if only_dates is not None else None

        out: dict[str, dict[str, bool]] = {}
        for idx in range(n):
            current = dates[idx]
            if wanted is not None and current not in wanted:
                continue
            if ascending:
                lo, hi = max(0, idx - self.window + 1), idx + 1
            else:
                lo, hi = idx, min(n, idx + self.window)
            if hi - lo < self.min_history:
                continue
            window = df_indicators.iloc[lo:hi]
            try:
                res = self.strategy.get_timing_result(
                    window, None, None, use_prev_day_signal=True, stock_code=code
                )
            except Exception as exc:
                self.errors += 1
                if len(self.error_samples) < 5:
                    self.error_samples.append(f"{code}@{current}: {exc}")
                continue
            out[current] = {"buy": bool(getattr(res, "is_buy", False)),
                            "sell": bool(getattr(res, "is_sell", False))}
        return out


def _worker(payload: dict):
    """子进程入口：给定股票分片，产出择时信号。"""
    codes = payload["codes"]
    from vector_bt import data

    klines = data.load_klines(
        db_path=payload["db_path"], codes=codes,
        start=payload["start"], end=payload["end"],
    )
    adapter = TimingAdapter(payload["timing"], window=payload["window"],
                            min_history=payload["min_history"])
    buys: list[tuple[str, str]] = []
    sells: list[tuple[str, str]] = []
    for code, df in klines.items():
        if df.empty:
            continue
        try:
            ind = adapter.prepare(df)
            res = adapter.scan(ind, code)
        except Exception:
            continue
        for date, flags in res.items():
            if flags["buy"]:
                buys.append((date, code))
            if flags["sell"]:
                sells.append((date, code))
    return buys, sells, adapter.errors


def build_timing_matrices(
    timing: str,
    codes: list[str],
    start: str,
    end: str,
    *,
    db_path=None,
    jobs: int = 1,
    window: int = 180,
    min_history: int = 60,
    verbose: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成 (买点矩阵, 卖点矩阵)：index=日期, columns=股票, 值为 bool。"""
    import multiprocessing as mp

    from vector_bt import data

    n_chunks = max(1, min(jobs, 8))
    chunks = data.chunked(codes, n_chunks)
    tasks = [
        {"codes": c, "start": start, "end": end, "timing": timing,
         "window": window, "min_history": min_history, "db_path": str(db_path) if db_path else None}
        for c in chunks if c
    ]

    all_buys: list[tuple[str, str]] = []
    all_sells: list[tuple[str, str]] = []
    errors = 0
    if jobs <= 1 or len(tasks) == 1:
        for t in tasks:
            b, s, e = _worker(t)
            all_buys += b
            all_sells += s
            errors += e
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=min(jobs, len(tasks))) as pool:
            for b, s, e in pool.imap_unordered(_worker, tasks):
                all_buys += b
                all_sells += s
                errors += e

    def _to_wide(rows: list[tuple[str, str]]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows, columns=["date", "code"]).drop_duplicates()
        frame["v"] = True
        wide = frame.pivot_table(index="date", columns="code", values="v", aggfunc="max")
        wide.index = pd.to_datetime(wide.index)
        return wide.sort_index().fillna(False).astype(bool)

    if verbose:
        print(f"  择时 {TIMING_NAMES.get(timing, timing)}：买点 {len(all_buys):,} 个、"
              f"卖点 {len(all_sells):,} 个（异常 {errors}）", flush=True)
    return _to_wide(all_buys), _to_wide(all_sells)


class TimingEvaluator:
    """按需评估择时信号（带缓存）。

    为什么不做全量矩阵：择时策略要在 5000 只 × 2800 天上评估，成本与一个选股策略
    同量级（约 1 小时）。而实际只需要两类点：

    * **进场**：选股信号命中的 (股票, 日期) —— 一年几百个；
    * **离场**：持仓期间的 (股票, 日期) —— 每笔约 10 天。

    因此改为按需评估 + 每只股票的指标只算一次，成本从小时级降到秒级。
    """

    def __init__(self, name: str = "turtle", *, window: int = 180, min_history: int = 60,
                 config: dict | None = None, db_path=None):
        self.adapter = TimingAdapter(name, config=config, window=window, min_history=min_history)
        self.name = name
        self.db_path = db_path
        self._ind: dict[str, pd.DataFrame] = {}
        self._pos: dict[str, dict[str, int]] = {}
        self._cache: dict[tuple[str, str], dict[str, bool]] = {}
        self.stats = {"buy_checks": 0, "sell_checks": 0, "codes": 0}

    def _load(self, code: str) -> bool:
        if code in self._ind:
            return True
        from vector_bt import data

        klines = data.load_klines(db_path=self.db_path, codes=[code])
        df = klines.get(code)
        if df is None or df.empty:
            self._ind[code] = pd.DataFrame()
            return False
        ind = self.adapter.prepare(df)
        self._ind[code] = ind
        dates = [str(v)[:10] for v in ind["date"].tolist()]
        self._pos[code] = {d: i for i, d in enumerate(dates)}
        self.stats["codes"] += 1
        return True

    def _eval(self, code: str, date: str) -> dict[str, bool]:
        key = (code, date)
        if key in self._cache:
            return self._cache[key]
        if not self._load(code):
            self._cache[key] = {"buy": False, "sell": False}
            return self._cache[key]
        ind = self._ind[code]
        idx = self._pos[code].get(date)
        if idx is None:
            self._cache[key] = {"buy": False, "sell": False}
            return self._cache[key]
        dates = [str(v)[:10] for v in ind["date"].tolist()]
        ascending = dates[0] <= dates[-1]
        n = len(ind)
        if ascending:
            lo, hi = max(0, idx - self.adapter.window + 1), idx + 1
        else:
            lo, hi = idx, min(n, idx + self.adapter.window)
        if hi - lo < self.adapter.min_history:
            flags = {"buy": False, "sell": False}
        else:
            try:
                res = self.adapter.strategy.get_timing_result(
                    ind.iloc[lo:hi], None, None, use_prev_day_signal=True, stock_code=code
                )
                flags = {"buy": bool(getattr(res, "is_buy", False)),
                         "sell": bool(getattr(res, "is_sell", False))}
            except Exception:
                flags = {"buy": False, "sell": False}
        self._cache[key] = flags
        return flags

    # 供引擎调用的两个入口
    def is_buy(self, code: str, date: str) -> bool:
        self.stats["buy_checks"] += 1
        return self._eval(code, date)["buy"]

    def is_sell(self, code: str, date: str) -> bool:
        self.stats["sell_checks"] += 1
        return self._eval(code, date)["sell"]
