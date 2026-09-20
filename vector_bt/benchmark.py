"""基准指数：抓取沪深300 等指数日线并本地缓存。

基准是判断"策略到底有没有 alpha"的前提 —— 组合赚 25% 而同期指数涨 30%，
那不叫有效，只叫跟涨。

数据源用 akshare 的新浪指数接口（``stock_zh_index_daily``），
东财接口在本机受代理干扰不稳定，故只用前者。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

DEFAULT_BENCHMARK = "000300"
from vector_bt.paths import CACHE_DIR

DEFAULT_CACHE_DIR = CACHE_DIR

# 常用指数，避免每次都记前缀
INDEX_ALIASES = {
    "000300": "sh000300",   # 沪深300
    "000001": "sh000001",   # 上证指数
    "000905": "sh000905",   # 中证500
    "000852": "sh000852",   # 中证1000
    "399006": "sz399006",   # 创业板指
    "399001": "sz399001",   # 深证成指
}


def _normalize_symbol(symbol: str) -> str:
    """把 000300 / sh000300 统一成 akshare 需要的带前缀代码。"""
    text = str(symbol).strip().lower()
    if text in INDEX_ALIASES:
        return INDEX_ALIASES[text]
    if text[:2] in {"sh", "sz", "bj"}:
        return text
    return ("sh" if text.startswith("0") else "sz") + text


def _fetch(symbol: str) -> pd.Series:
    import akshare as ak

    frame = ak.stock_zh_index_daily(symbol=_normalize_symbol(symbol))
    if frame is None or frame.empty:
        raise RuntimeError(f"指数 {symbol} 返回空数据")
    series = pd.Series(
        frame["close"].astype(float).values,
        index=pd.to_datetime(frame["date"]),
        name=symbol,
    )
    return series.sort_index()


def load_benchmark(
    symbol: str = DEFAULT_BENCHMARK,
    start: str | None = None,
    end: str | None = None,
    *,
    cache_dir: Path | str | None = None,
    refresh: bool = False,
) -> pd.Series:
    """返回基准收盘价序列（index 为 DatetimeIndex，升序）。

    缓存策略：本地 ``.cache/benchmark_<symbol>.pkl`` 覆盖所需区间就直接用，
    否则重新抓取并与缓存合并（增量补数）。
    """
    cache_root = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_file = cache_root / f"benchmark_{str(symbol).strip().lower()}.pkl"

    cached: pd.Series | None = None
    if cache_file.exists() and not refresh:
        try:
            cached = pd.read_pickle(cache_file)
        except Exception:  # 缓存损坏时忽略，走重新抓取
            cached = None

    need_fetch = cached is None
    if cached is not None and (start or end):
        lo = pd.to_datetime(start) if start else cached.index.min()
        hi = pd.to_datetime(end) if end else cached.index.max()
        # 端点不在缓存内就认为覆盖不足（指数数据本身是全量的，代价很小）
        if cached.index.min() > lo or cached.index.max() < hi:
            need_fetch = True

    if need_fetch:
        try:
            fresh = _fetch(symbol)
        except Exception as exc:
            if cached is None:
                raise
            logging.getLogger(__name__).warning("基准 %s 抓取失败，改用缓存: %s", symbol, exc)
            fresh = cached.iloc[0:0]
        merged = fresh if cached is None else pd.concat([cached, fresh])
        series = merged[~merged.index.duplicated(keep="last")].sort_index()
        series.to_pickle(cache_file)
    else:
        series = cached

    if start:
        series = series[series.index >= pd.to_datetime(start)]
    if end:
        series = series[series.index <= pd.to_datetime(end)]
    return series


def benchmark_returns(
    series: pd.Series, dates: pd.DatetimeIndex
) -> pd.Series:
    """把基准对齐到回测日期并算日收益（基准缺失日按 0 处理，避免污染净值）。"""
    aligned = series.reindex(dates).ffill()
    returns = aligned.pct_change(fill_method=None)
    returns.iloc[0] = 0.0
    return returns.fillna(0.0)
