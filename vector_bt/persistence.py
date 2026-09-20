"""把向量化回测结果写入 KHunter 的既有回测表，让 Web 端「回测历史」能看到。

背景：KHunter 原有的事件驱动回测结果存在 ``backtest_result`` / ``backtest_trade``
两张表里，前端「策略回测」「回测历史」页面读的就是这两张表。此前向量化回测的结果
只存在于本模块内存和 CSV 里，界面上看不到 —— 本模块负责补上这一步。

字段单位（与原有引擎保持一致，勿改）
------------------------------------
* ``win_rate`` / ``total_return`` / ``max_drawdown`` / ``avg_return`` 都是**百分数**
  （42.56 表示 42.56%）
* ``max_drawdown`` 取**正数**（原引擎用 ``abs(min(drawdown))``）

逐笔明细的买入金额为等权近似：``initial_capital / max_daily_buys``，
数量按 100 股整手向下取整。原引擎有真实资金占用模拟，两者口径不同，
因此 ``backtest_name`` 会带「向量化」前缀以便区分。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from vector_bt.engine import BacktestResult
from vector_bt.paths import DB_PATH

EXIT_REASON_MAP = {"止损": "stop_loss", "止盈": "take_profit", "持有到期": "hold_expired"}


def _stock_names(db_path: Path) -> dict[str, str]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return {str(c): (n or "") for c, n in con.execute("select code, name from stock_basic")}
    finally:
        con.close()


def save_backtest_result(
    result: BacktestResult,
    *,
    backtest_name: str | None = None,
    support_level_method: str = "vector_bt",
    initial_capital: float | None = None,
    max_daily_buys: int = 8,
    db_path: Path | str | None = None,
) -> int:
    """写入一条回测结果及其逐笔明细，返回 ``backtest_result.id``。"""
    path = Path(db_path) if db_path else DB_PATH
    trades = result.trades_frame
    total = int(len(trades))
    wins = int((trades["net_return"] > 0).sum()) if total else 0
    losses = total - wins
    win_rate = (wins / total * 100) if total else 0.0
    avg_return = float(trades["net_return"].mean() * 100) if total else 0.0
    capital = float(initial_capital or 300_000)
    name = backtest_name or f"向量化回测 {result.start}~{result.end}"

    con = sqlite3.connect(str(path), timeout=60)
    con.execute("pragma busy_timeout=60000")
    try:
        cur = con.execute(
            """
            INSERT INTO backtest_result (
                strategy_name, support_level_method, backtest_name, start_date, end_date,
                total_trades, win_trades, loss_trades, win_rate, avg_return,
                total_return, max_return, min_return, profit_factor, profit_loss_ratio,
                max_drawdown, sharpe_ratio, volatility, sortino_ratio, avg_hold_days,
                initial_capital, final_capital, created_at
            ) VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?)
            """,
            (
                result.strategy, support_level_method, name, result.start, result.end,
                total, wins, losses, win_rate, avg_return,
                result.total_return * 100,
                float(trades["net_return"].max() * 100) if total else 0.0,
                float(trades["net_return"].min() * 100) if total else 0.0,
                float(result.profit_factor) if result.profit_factor != float("inf") else 0.0,
                float(result.profit_factor) if result.profit_factor != float("inf") else 0.0,
                abs(result.max_drawdown) * 100,
                result.sharpe, 0.0, 0.0, result.avg_hold_days,
                capital, result.final_value,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        result_id = int(cur.lastrowid)

        if total:
            names = _stock_names(path)
            buy_amount = capital / max(1, max_daily_buys)
            rows = []
            for row in trades.itertuples(index=False):
                price = float(row.entry_price)
                quantity = int(buy_amount // price // 100 * 100) if price > 0 else 0
                rows.append(
                    (
                        result_id,
                        str(row.code),
                        names.get(str(row.code), ""),
                        str(row.signal_date)[:10],
                        str(row.entry_date)[:10],
                        price,
                        buy_amount,
                        quantity,
                        str(row.exit_date)[:10],
                        float(row.exit_price),
                        EXIT_REASON_MAP.get(str(row.exit_reason), "hold_expired"),
                        float(row.net_return) * 100,
                        float(row.net_return) * buy_amount,
                        int(row.hold_days),
                    )
                )
            con.executemany(
                """
                INSERT INTO backtest_trade (
                    result_id, stock_code, stock_name, selection_date, buy_date,
                    buy_price, buy_amount, quantity, sell_date, sell_price,
                    sell_type, return_rate, profit_loss, hold_days, trade_type
                ) VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,'normal')
                """,
                rows,
            )
        con.commit()
        return result_id
    finally:
        con.close()
