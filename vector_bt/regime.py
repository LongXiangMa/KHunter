"""市场状态分层：判断策略的收益是"能力"还是"特定行情下的运气"。

多区间验证只能说明"某段时间有效"，但没法回答"为什么有效、下次什么条件该用它"。
本模块把每个交易日打上行情标签，再看策略在不同标签下的表现。

状态定义（刻意保持可解释，不做黑箱聚类）：

* 趋势：沪深300 的 20 日收益，> +3% 记为上行、< -3% 记为下行、中间为震荡
* 活跃度：全市场涨停家数占比的 5 日均值，按区间中位数切成高/低

涨停家数占比是形态类策略最相关的环境变量 —— "涨停横盘""涨停回马枪"这类策略
在涨停稀缺的环境里几乎没有标的可选。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from vector_bt.benchmark import DEFAULT_CACHE_DIR
from vector_bt.data import connect_ro

TREND_WINDOW = 20
BREADTH_WINDOW = 5
TREND_UP = 0.03    # 20 日涨幅超过 3% 视为上行
TREND_DOWN = -0.03


def load_market_breadth(
    start: str,
    end: str,
    *,
    db_path: str | Path | None = None,
    cache_dir: Path | str | None = None,
    refresh: bool = False,
) -> pd.DataFrame:
    """全市场宽度：每日涨停家数占比与平均涨跌幅。

    用 SQL 窗口函数一次算完（387 万行约 15 秒），结果落盘缓存。
    """
    cache_root = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_file = cache_root / f"breadth_{start}_{end}.pkl"
    if cache_file.exists() and not refresh:
        try:
            return pd.read_pickle(cache_file)
        except Exception:
            pass

    sql = """
        select date,
               count(*) as n,
               sum(case when close / prev - 1 >= 0.095 then 1 else 0 end) * 1.0 / count(*) as limit_up_ratio,
               sum(case when close / prev - 1 <= -0.095 then 1 else 0 end) * 1.0 / count(*) as limit_down_ratio,
               avg(close / prev - 1) as avg_change
        from (select code, date, close,
                     lag(close) over (partition by code order by date) as prev
              from stock_kline
              where date >= ? and date <= ?)
        where prev is not null
        group by date
        order by date
    """
    with connect_ro(db_path) as con:
        frame = pd.read_sql(sql, con, params=[start, end])
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.set_index("date").sort_index()
    frame.to_pickle(cache_file)
    return frame


def market_features(index_close: pd.Series, breadth: pd.DataFrame) -> pd.DataFrame:
    """把指数走势与市场宽度合成特征表。"""
    close = index_close.sort_index()
    ret = close.pct_change()
    features = pd.DataFrame(index=close.index)
    features["index_close"] = close
    features["ret_20d"] = close.pct_change(TREND_WINDOW)
    features["vol_20d"] = ret.rolling(TREND_WINDOW).std() * np.sqrt(252)
    features = features.join(breadth, how="left")
    features["limit_up_ma"] = features["limit_up_ratio"].rolling(BREADTH_WINDOW).mean()
    features["limit_down_ma"] = features["limit_down_ratio"].rolling(BREADTH_WINDOW).mean()
    features["breadth_n"] = features["n"]
    return features


def classify(features: pd.DataFrame, min_stocks: int = 1000) -> pd.DataFrame:
    """给每个交易日打标签：趋势 / 活跃度 / 综合状态。"""
    out = features.copy()

    # 数据覆盖不足的早期日期（全市场只有几十只股票）不参与统计
    valid = out["breadth_n"].fillna(0) >= min_stocks

    trend = pd.Series("震荡", index=out.index, dtype=object)
    trend[out["ret_20d"] > TREND_UP] = "上行"
    trend[out["ret_20d"] < TREND_DOWN] = "下行"
    trend[~valid] = "数据不足"
    out["trend"] = trend

    threshold = out.loc[valid, "limit_up_ma"].median()
    activity = pd.Series("低活跃", index=out.index, dtype=object)
    activity[out["limit_up_ma"] >= threshold] = "高活跃"
    activity[~valid] = "数据不足"
    out["activity"] = activity
    out["activity_threshold"] = threshold
    out["regime"] = out["trend"] + "·" + out["activity"]
    out.loc[~valid, "regime"] = "数据不足"
    return out


def strategy_daily_returns(equity_file: Path) -> pd.Series:
    """从净值曲线文件算出策略日收益。"""
    frame = pd.read_csv(equity_file, encoding="utf-8-sig", index_col=0, parse_dates=True)
    column = "策略净值" if "策略净值" in frame.columns else frame.columns[0]
    return frame[column].pct_change().fillna(0.0)


def benchmark_daily_returns(equity_file: Path) -> pd.Series:
    frame = pd.read_csv(equity_file, encoding="utf-8-sig", index_col=0, parse_dates=True)
    if "基准净值" not in frame.columns:
        return pd.Series(dtype=float)
    return frame["基准净值"].pct_change().fillna(0.0)


def regime_performance(
    run_dir: Path | str,
    regimes: pd.DataFrame,
    *,
    metric: str = "excess",
) -> pd.DataFrame:
    """按状态统计每个策略的表现。

    返回长表：策略 × 状态 → 交易日数 / 年化超额 / 信息比率 / 日胜率。
    """
    run_path = Path(run_dir)
    files = sorted(run_path.glob("equity_*.csv"))
    if not files:
        raise FileNotFoundError(f"{run_path} 下没有 equity_*.csv")

    bench = benchmark_daily_returns(files[0])
    rows: list[dict] = []
    for path in files:
        name = path.stem.replace("equity_", "")
        daily = strategy_daily_returns(path)
        excess = (daily - bench.reindex(daily.index).fillna(0.0)) if len(bench) else daily
        joined = pd.DataFrame({"excess": excess}).join(regimes[["trend", "activity", "regime"]], how="inner")
        for regime, group in joined.groupby("regime"):
            if regime == "数据不足" or len(group) < 20:
                continue
            series = group["excess"]
            std = float(series.std(ddof=1)) if len(series) > 1 else 0.0
            rows.append(
                {
                    "策略": name,
                    "状态": regime,
                    "交易日数": int(len(series)),
                    "年化超额": float(series.mean() * 252),
                    "信息比率": float(series.mean() / std * np.sqrt(252)) if std > 0 else 0.0,
                    "日胜率": float((series > 0).mean()),
                }
            )
    return pd.DataFrame(rows)


def rolling_threshold(
    features: pd.DataFrame, window: int = 250, min_periods: int = 120
) -> pd.Series:
    """滚动活跃度阈值：过去 N 个交易日涨停占比 5 日均值的中位数。

    只用当日之前的数据，无未来函数。相比固定阈值，它能自动跟随市场长期水平漂移
    （实测：样本内中位数 1.15% 的固定阈值到样本外会命中 96.6% 的交易日，等于失效）。
    """
    return features["limit_up_ma"].rolling(window, min_periods=min_periods).median()


def build_regime_filter(
    dates: pd.DatetimeIndex | None = None,
    *,
    market_start: str,
    market_end: str,
    method: str = "rolling",
    window: int = 250,
    fixed_threshold: float | None = None,
    min_stocks: int = 1000,
    benchmark: str = "000300",
    db_path: str | Path | None = None,
    cache_dir: Path | str | None = None,
) -> tuple[pd.Series, pd.DataFrame]:
    """生成"允许开仓"的日掩码。

    method="rolling"：用滚动阈值（推荐，实盘可用）
    method="fixed"  ：用固定阈值（需显式给出 fixed_threshold，仅用于对照实验）

    返回 (allow_mask, features)；allow_mask 为 True 表示该日允许开新仓。
    """
    from vector_bt.benchmark import load_benchmark

    index_close = load_benchmark(benchmark, market_start, market_end, cache_dir=cache_dir)
    breadth = load_market_breadth(
        market_start, market_end, db_path=db_path, cache_dir=cache_dir
    )
    feats = market_features(index_close, breadth)
    valid = feats["n"].fillna(0) >= min_stocks

    if method == "fixed":
        if fixed_threshold is None:
            raise ValueError("method='fixed' 时必须提供 fixed_threshold")
        threshold = pd.Series(float(fixed_threshold), index=feats.index)
    else:
        threshold = rolling_threshold(feats, window=window)

    allow = pd.Series(
        (feats["limit_up_ma"] >= threshold) & valid, index=feats.index
    ).fillna(False)
    if dates is not None:
        allow = allow.reindex(pd.DatetimeIndex(dates)).fillna(False)
    return allow, feats


def daily_regime_status(
    *,
    end: str,
    lookback_days: int = 400,
    window: int = 250,
    benchmark: str = "000300",
    db_path: str | Path | None = None,
) -> dict:
    """最新交易日的状态快照，用于每日实盘决策。"""
    start = (pd.to_datetime(end) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    _, feats = build_regime_filter(
        pd.DatetimeIndex([pd.to_datetime(end)]),
        market_start=start,
        market_end=end,
        method="rolling",
        window=window,
        benchmark=benchmark,
        db_path=db_path,
    )
    latest = feats.dropna(subset=["limit_up_ma"]).iloc[-1]
    threshold = rolling_threshold(feats, window=window).iloc[-1]
    return {
        "date": feats.index[-1].strftime("%Y-%m-%d"),
        "limit_up_ma": float(latest["limit_up_ma"]),
        "threshold": float(threshold) if pd.notna(threshold) else float("nan"),
        "active": bool(pd.notna(threshold) and latest["limit_up_ma"] >= threshold),
        "index_close": float(latest["index_close"]),
        "ret_20d": float(latest["ret_20d"]) if pd.notna(latest["ret_20d"]) else float("nan"),
    }
