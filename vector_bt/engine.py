"""向量化组合回测引擎。

设计参考 QuantMind 的 ``VectorizedBacktestEngine``（见其
``backend/shared/vectorized_backtest/engine.py``），并按 KHunter 的交易规则改写：

    QuantMind：每日 TopK 轮动 + 等权
    KHunter  ：信号日买入、持有 N 日、每日买入上限、止损/止盈

核心思想（也是它快的原因）：把信号和价格都整理成
``DataFrame(index=日期, columns=股票)`` 的宽表，用矩阵运算一次性算完，
避免"每天循环 5000 只股票"。

交易时点约定（回测里最容易出错的地方，这里明确写死）
--------------------------------------------------
1. 信号在 **t 日收盘后**才确定；
2. 因此在 **t+1 日收盘**买入（不使用 t 日收盘价，避免未来函数）；
3. 持有 hold_period 个交易日，即赚取 t+2 … t+1+hold 的收盘到收盘收益；
4. 止损/止盈用持有期内每天的 low/high 判定，同一天同时触发时按**止损**处理（偏保守）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd

from vector_bt.benchmark import benchmark_returns


@dataclass
class BacktestConfig:
    """回测参数。费率口径对齐 KHunter/A 股实盘：佣金万2.5(最低5元)+过户费万0.1+印花税万5(仅卖出)。"""

    initial_capital: float = 300_000.0
    hold_period: int = 10
    max_daily_buys: int = 8
    stop_loss: float = -0.07
    take_profit: float = 0.21
    commission: float = 0.00025
    min_commission: float = 5.0
    stamp_duty: float = 0.0005
    transfer_fee: float = 0.00001
    slippage: float = 0.0005
    limit_threshold: float = 0.095


@dataclass
class BacktestResult:
    strategy: str
    start: str
    end: str
    total_return: float
    annual_return: float
    sharpe: float
    max_drawdown: float
    daily_win_rate: float
    trades: int
    trade_win_rate: float
    avg_trade_return: float
    profit_factor: float
    avg_hold_days: float
    final_value: float
    benchmark_name: str = ""
    benchmark_total_return: float = 0.0
    benchmark_annual_return: float = 0.0
    excess_annual_return: float = 0.0
    information_ratio: float = 0.0
    tracking_error: float = 0.0
    beta: float = 0.0
    alpha: float = 0.0
    equity_curve: pd.Series = field(repr=False, default_factory=pd.Series)
    trades_frame: pd.DataFrame = field(repr=False, default_factory=pd.DataFrame)
    benchmark_curve: pd.Series = field(repr=False, default_factory=pd.Series)

    def as_row(self) -> dict:
        return {
            "策略": self.strategy,
            "累计收益": self.total_return,
            "年化收益": self.annual_return,
            "基准年化": self.benchmark_annual_return,
            "超额年化": self.excess_annual_return,
            "夏普": self.sharpe,
            "信息比率": self.information_ratio,
            "最大回撤": self.max_drawdown,
            "日胜率": self.daily_win_rate,
            "交易笔数": self.trades,
            "逐笔胜率": self.trade_win_rate,
            "平均单笔": self.avg_trade_return,
            "盈亏比": self.profit_factor,
            "平均持有天数": self.avg_hold_days,
            "跟踪误差": self.tracking_error,
            "Beta": self.beta,
            "Alpha": self.alpha,
        }


def _pick_entries(
    signals: pd.DataFrame,
    changes: pd.DataFrame,
    config: BacktestConfig,
) -> dict[pd.Timestamp, list[str]]:
    """按日挑出入选股票：按信号分数排序，剔除涨停/停牌，限制每日买入上限。"""
    picks: dict[pd.Timestamp, list[str]] = {}
    limit_mask = changes.abs() >= config.limit_threshold
    for date in signals.index:
        row = signals.loc[date].dropna()
        if row.empty:
            continue
        blocked = limit_mask.loc[date] if date in limit_mask.index else None
        if blocked is not None:
            row = row[~blocked.reindex(row.index).fillna(False)]
        if row.empty:
            continue
        ordered = row.sort_values(ascending=False, kind="stable")
        picks[date] = list(ordered.index[: config.max_daily_buys])
    return picks


@dataclass
class Trade:
    """一笔完整交易。退出规则：先判止损，再判止盈，都没触发则持有到期。"""

    code: str
    col: int
    signal_idx: int
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    exit_reason: str

    @property
    def gross_return(self) -> float:
        return self.exit_price / self.entry_price - 1

    @property
    def hold_days(self) -> int:
        return self.exit_idx - self.entry_idx


def _simulate_trades(
    picks: dict[pd.Timestamp, list[str]],
    dates: pd.DatetimeIndex,
    columns: pd.Index,
    close_np: np.ndarray,
    high_np: np.ndarray,
    low_np: np.ndarray,
    config: BacktestConfig,
) -> list[Trade]:
    """逐笔模拟并确定退出日。

    这里必须逐笔走，而不是"固定持有 N 天"的矩阵写法 —— 因为止损/止盈是
    路径依赖的：不逐笔判断，参数扫描出来的所有组合会得到同一条净值曲线
    （开发中踩过：4 个不同止损/止盈组合结果完全一致）。

    代价可控：一年 5000 只股票下成交笔数在几百到几千量级，逐笔循环远小于矩阵运算。
    """
    date_of = {d: i for i, d in enumerate(dates)}
    col_of = {code: i for i, code in enumerate(columns)}
    trades: list[Trade] = []
    n = len(dates)
    stop_factor = 1 + config.stop_loss
    take_factor = 1 + config.take_profit

    for sig_date, codes in picks.items():
        sig_idx = date_of.get(sig_date)
        if sig_idx is None:
            continue
        entry_idx = sig_idx + 1          # t+1 收盘买入
        if entry_idx >= n:
            continue
        last_idx = min(entry_idx + config.hold_period, n - 1)
        if last_idx <= entry_idx:
            continue
        for code in codes:
            col = col_of.get(code)
            if col is None:
                continue
            entry_price = close_np[entry_idx, col]
            if not np.isfinite(entry_price) or entry_price <= 0:
                continue
            exit_idx = last_idx
            exit_price = close_np[last_idx, col]
            reason = "持有到期"
            for step in range(entry_idx + 1, last_idx + 1):
                low_v = low_np[step, col]
                high_v = high_np[step, col]
                if np.isfinite(low_v) and low_v <= entry_price * stop_factor:
                    exit_idx, exit_price, reason = step, entry_price * stop_factor, "止损"
                    break
                if np.isfinite(high_v) and high_v >= entry_price * take_factor:
                    exit_idx, exit_price, reason = step, entry_price * take_factor, "止盈"
                    break
            if not np.isfinite(exit_price):
                continue
            trades.append(
                Trade(
                    code=code,
                    col=col,
                    signal_idx=sig_idx,
                    entry_idx=entry_idx,
                    exit_idx=exit_idx,
                    entry_price=float(entry_price),
                    exit_price=float(exit_price),
                    exit_reason=reason,
                )
            )
    return trades


def _hold_and_adjust(
    trades: list[Trade],
    dates: pd.DatetimeIndex,
    columns: pd.Index,
    close_np: np.ndarray,
    config: BacktestConfig,
) -> tuple[pd.DataFrame, np.ndarray]:
    """由成交清单构造持仓矩阵，并给出"退出日价格修正"。

    持仓矩阵用"差分 + 累加"：每笔在 entry_idx+1 日计入（开始赚收益）、
    在 exit_idx+1 日移除。

    价格修正：若某笔在退出日是按止损/止盈价成交，那么当天的收益不该用收盘价算，
    差额记进 adjust 向量（单位与组合日收益一致）。
    """
    n, m = len(dates), len(columns)
    delta = np.zeros((n, m), dtype=np.float32)
    adjust = np.zeros(n, dtype=np.float64)
    day_count = np.zeros(n, dtype=np.float64)

    for trade in trades:
        begin = trade.entry_idx + 1          # 次日起计收益
        if begin >= n:
            continue
        delta[begin, trade.col] += 1.0
        if trade.exit_idx + 1 < n:
            delta[trade.exit_idx + 1, trade.col] -= 1.0

        if trade.exit_reason != "持有到期":
            prev_close = close_np[trade.exit_idx - 1, trade.col]
            day_close = close_np[trade.exit_idx, trade.col]
            if np.isfinite(prev_close) and prev_close > 0 and np.isfinite(day_close):
                actual = trade.exit_price / prev_close - 1
                close_based = day_close / prev_close - 1
                adjust[trade.exit_idx] += actual - close_based
        day_count[trade.exit_idx] += 1

    held = np.cumsum(delta, axis=0) > 0
    hold_frame = pd.DataFrame(held.astype(np.float32), index=dates, columns=columns)
    return hold_frame, adjust


def _per_trade_frame(
    trades: list[Trade],
    dates: pd.DatetimeIndex,
    config: BacktestConfig,
) -> pd.DataFrame:
    """逐笔统计（止损止盈价已由 _simulate_trades 判定）。"""
    records: list[dict] = []
    round_trip_cost = (
        config.commission * 2
        + config.slippage * 2
        + config.stamp_duty
        + config.transfer_fee
    )
    for trade in trades:
        records.append(
            {
                "code": trade.code,
                "signal_date": dates[trade.signal_idx],
                "entry_date": dates[trade.entry_idx],
                "exit_date": dates[trade.exit_idx],
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "gross_return": trade.gross_return,
                "net_return": trade.gross_return - round_trip_cost,
                "exit_reason": trade.exit_reason,
                "hold_days": trade.hold_days,
            }
        )
    return pd.DataFrame.from_records(records)


def run_backtest(
    signals: pd.DataFrame,
    close: pd.DataFrame,
    *,
    high: pd.DataFrame | None = None,
    low: pd.DataFrame | None = None,
    benchmark: pd.Series | None = None,
    benchmark_name: str = "",
    strategy: str = "unnamed",
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """执行一次向量化回测。

    signals：宽表，index=日期，columns=股票代码，value=信号分数（无信号为 NaN）
    close/high/low：同形状的价格宽表
    benchmark：基准指数收盘价序列（可选）。传入后会计算超额收益 / 信息比率 / Beta / Alpha
    """
    cfg = config or BacktestConfig()
    if signals.empty or close.empty:
        raise ValueError("信号或价格为空，无法回测")

    dates = pd.DatetimeIndex(sorted(close.index))
    columns = close.columns
    sig = signals.reindex(index=dates, columns=columns)
    px = close.reindex(index=dates, columns=columns).ffill()
    hi = (high if high is not None else close).reindex(index=dates, columns=columns).ffill()
    lo = (low if low is not None else close).reindex(index=dates, columns=columns).ffill()

    asset_returns = px.pct_change(fill_method=None)
    changes = asset_returns  # 涨跌停判定用当日涨跌幅

    picks = _pick_entries(sig, changes.fillna(0.0), cfg)
    close_np = px.to_numpy(dtype=float)
    high_np = hi.to_numpy(dtype=float)
    low_np = lo.to_numpy(dtype=float)
    trades = _simulate_trades(picks, dates, columns, close_np, high_np, low_np, cfg)
    held, adjust = _hold_and_adjust(trades, dates, columns, close_np, cfg)

    weight_sum = held.sum(axis=1)
    weights = held.div(weight_sum.where(weight_sum > 0, 1), axis=0)

    gross_returns = (weights * asset_returns).sum(axis=1).fillna(0.0)
    # 止损/止盈在退出日是按触发价成交的，把"收盘口径"换成"成交口径"的差额补回来
    if len(trades):
        gross_returns = gross_returns + (adjust / weight_sum.where(weight_sum > 0, np.nan)).fillna(0.0)

    # 交易成本：按权重变动拆分买卖两侧（买卖费率不对称）
    weight_diff = weights.diff().fillna(weights.iloc[0] if len(weights) else 0.0)
    buy_turnover = weight_diff.clip(lower=0).sum(axis=1)
    sell_turnover = (-weight_diff.clip(upper=0)).sum(axis=1)
    buy_cost = buy_turnover * (cfg.commission + cfg.slippage)
    sell_cost = sell_turnover * (cfg.commission + cfg.slippage + cfg.stamp_duty + cfg.transfer_fee)
    # 最低佣金 5 元：用"每笔独立计算"无法向量化，这里按每日买入笔数补齐差额
    buys_per_day = pd.Series(
        {d: len(picks.get(d, [])) for d in dates}, index=dates
    ).astype(float)
    min_commission_gap = (
        (cfg.min_commission - cfg.initial_capital * buy_turnover / buys_per_day.replace(0, np.nan))
        .clip(lower=0)
        .fillna(0.0)
        * buys_per_day
        / cfg.initial_capital
    )
    net_returns = gross_returns - buy_cost - sell_cost - min_commission_gap

    equity = (1 + net_returns).cumprod() * cfg.initial_capital
    total_return = float(equity.iloc[-1] / cfg.initial_capital - 1) if len(equity) else 0.0
    years = max((dates[-1] - dates[0]).days / 365.25, 1e-6) if len(dates) > 1 else 1.0
    annual_return = float((1 + total_return) ** (1 / years) - 1) if years > 0 else 0.0
    daily_std = float(net_returns.std(ddof=1)) if len(net_returns) > 1 else 0.0
    sharpe = float((annual_return - 0.02) / (daily_std * np.sqrt(252))) if daily_std > 0 else 0.0
    drawdown = (equity - equity.cummax()) / equity.cummax()

    # --- 基准对比 ---------------------------------------------------------
    bm_total = bm_annual = excess_annual = info_ratio = tracking = beta = alpha = 0.0
    benchmark_curve = pd.Series(dtype=float)
    if benchmark is not None and len(benchmark) > 1:
        bm_daily = benchmark_returns(benchmark, dates)
        bm_total = float((1 + bm_daily).prod() - 1)
        bm_annual = float((1 + bm_total) ** (1 / years) - 1) if years > 0 else 0.0
        excess_annual = annual_return - bm_annual
        diff = (net_returns - bm_daily).astype(float)
        diff_std = float(diff.std(ddof=1)) if len(diff) > 1 else 0.0
        tracking = diff_std * np.sqrt(252)
        info_ratio = float(diff.mean() / diff_std * np.sqrt(252)) if diff_std > 0 else 0.0
        bm_var = float(bm_daily.var(ddof=1)) if len(bm_daily) > 1 else 0.0
        if bm_var > 0:
            cov = float(np.cov(net_returns.astype(float), bm_daily.astype(float))[0, 1])
            beta = cov / bm_var
        alpha = float((net_returns.mean() - beta * bm_daily.mean()) * 252)
        benchmark_curve = (1 + bm_daily).cumprod() * cfg.initial_capital

    trades_frame = _per_trade_frame(trades, dates, cfg)
    if not trades_frame.empty:
        wins = trades_frame[trades_frame["net_return"] > 0]["net_return"]
        losses = trades_frame[trades_frame["net_return"] <= 0]["net_return"]
        trade_win_rate = float(len(wins) / len(trades_frame))
        avg_trade_return = float(trades_frame["net_return"].mean())
        profit_factor = (
            float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")
        )
        avg_hold = float(trades_frame["hold_days"].mean())
    else:
        trade_win_rate = avg_trade_return = avg_hold = 0.0
        profit_factor = 0.0

    return BacktestResult(
        strategy=strategy,
        start=dates[0].strftime("%Y-%m-%d") if len(dates) else "",
        end=dates[-1].strftime("%Y-%m-%d") if len(dates) else "",
        total_return=total_return,
        annual_return=annual_return,
        sharpe=sharpe,
        max_drawdown=float(drawdown.min()) if len(drawdown) else 0.0,
        daily_win_rate=float((net_returns > 0).mean()) if len(net_returns) else 0.0,
        trades=int(len(trades_frame)),
        trade_win_rate=trade_win_rate,
        avg_trade_return=avg_trade_return,
        profit_factor=profit_factor,
        avg_hold_days=avg_hold,
        final_value=float(equity.iloc[-1]) if len(equity) else cfg.initial_capital,
        benchmark_name=benchmark_name,
        benchmark_total_return=bm_total,
        benchmark_annual_return=bm_annual,
        excess_annual_return=excess_annual,
        information_ratio=info_ratio,
        tracking_error=tracking,
        beta=beta,
        alpha=alpha,
        equity_curve=equity,
        trades_frame=trades_frame,
        benchmark_curve=benchmark_curve,
    )
