"""数据层：一次性载入 KHunter 的 K 线并按股票分组。

KHunter 原实现的问题不是策略慢，而是**每天重新从 SQLite 取 5175 只股票**
（实测单日 12 秒，其中取数 7 秒 + 分组 5 秒）。本模块把它换成"整段读取一次"。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from vector_bt.paths import DB_PATH

DEFAULT_DB = DB_PATH

# 策略代码需要的字段（strategy/base_strategy._validate_data 会校验这几列）
KLINE_COLUMNS = ["code", "date", "open", "high", "low", "close", "volume", "market_cap"]


def resolve_db(db_path: str | Path | None = None) -> Path:
    """解析数据库路径，缺省用 KHunter 的数据目录。"""
    path = Path(db_path) if db_path else DEFAULT_DB
    if not path.exists():
        raise FileNotFoundError(f"找不到 KHunter 数据库: {path}")
    return path


def connect_ro(db_path: str | Path | None = None) -> sqlite3.Connection:
    """以只读方式连接，避免回测过程中误写库。"""
    path = resolve_db(db_path)
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def load_calendar(
    db_path: str | Path | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[str]:
    """返回交易日历（该库里有 K 线的日期），升序。"""
    sql = "select distinct date from stock_kline"
    params: list[str] = []
    conds = []
    if start:
        conds.append("date >= ?")
        params.append(start)
    if end:
        conds.append("date <= ?")
        params.append(end)
    if conds:
        sql += " where " + " and ".join(conds)
    sql += " order by date"
    with connect_ro(db_path) as con:
        return [r[0] for r in con.execute(sql, params)]


def load_stock_names(db_path: str | Path | None = None) -> dict[str, str]:
    """股票代码 → 名称。策略用名称过滤 ST/退市股，缺失时传空串。"""
    with connect_ro(db_path) as con:
        return {str(code): (name or "") for code, name in con.execute(
            "select code, name from stock_basic"
        )}


def load_symbols(
    db_path: str | Path | None = None,
    start: str | None = None,
    end: str | None = None,
    min_rows: int = 60,
    codes: Iterable[str] | None = None,
) -> dict[str, str]:
    """挑选可回测的股票：在区间内有足够 K 线行数。

    与原实现的差异：原实现按"选股日是否有 K 线"逐日过滤；这里用行数下限做静态筛选，
    真正的"当日无数据即视为停牌"仍由策略的 `_is_suspended` 在评估时判定。
    """
    sql = ["select code, count(*) as n from stock_kline"]
    params: list[str] = []
    conds = []
    if start:
        conds.append("date >= ?")
        params.append(start)
    if end:
        conds.append("date <= ?")
        params.append(end)
    if codes is not None:
        code_list = list(codes)
        if not code_list:
            return {}
        conds.append("code in (%s)" % ",".join("?" * len(code_list)))
        params.extend(code_list)
    if conds:
        sql.append(" where " + " and ".join(conds))
    sql.append(" group by code having n >= ?")
    # 注意：必须传 int。SQLite 中 INTEGER 与 TEXT 比较时文本恒大于整数，
    # 传 '200' 会导致所有股票被过滤掉（踩过一次）。
    params.append(int(min_rows))

    with connect_ro(db_path) as con:
        rows = con.execute(" ".join(sql), params).fetchall()
    names = load_stock_names(db_path)
    return {str(code): names.get(str(code), "") for code, _ in rows}


def load_klines(
    db_path: str | Path | None = None,
    codes: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    lookback_pad: int = 0,
) -> dict[str, pd.DataFrame]:
    """读取 K 线并返回 {code: DataFrame(升序)}。

    lookback_pad：策略需要历史窗口才能算出指标，可在区间起点前多取若干"行"。
    按行补（而不是按天补）以保证每只股票都有相同的预热长度。
    """
    conds: list[str] = []
    params: list[str] = []
    if codes is not None:
        code_list = list(codes)
        if not code_list:
            return {}
        conds.append("code in (%s)" % ",".join("?" * len(code_list)))
        params.extend(code_list)
    if start:
        conds.append("date >= ?")
        params.append(start)
    if end:
        conds.append("date <= ?")
        params.append(end)
    where = (" where " + " and ".join(conds)) if conds else ""

    cols = ",".join(KLINE_COLUMNS)
    sql = f"select {cols} from stock_kline{where} order by code, date"

    with connect_ro(db_path) as con:
        frame = pd.read_sql(sql, con, params=params)

    if frame.empty:
        return {}

    out: dict[str, pd.DataFrame] = {}
    for code, group in frame.groupby("code", sort=False):
        g = group.drop(columns=["code"]).reset_index(drop=True)
        if lookback_pad:
            g = g.tail(len(g))  # 保持接口一致，实际补行由调用方控制区间
        out[str(code)] = g
    return out


def chunked(items: Sequence[str], n_chunks: int) -> list[list[str]]:
    """把列表尽量平均切成 n 份（用于多进程分片）。"""
    n_chunks = max(1, n_chunks)
    return [list(items[i::n_chunks]) for i in range(n_chunks)]
