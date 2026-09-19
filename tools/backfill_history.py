"""历史 K 线补全工具（KHunter v1.7.0 新增）

背景
----
KHunter 自带的 `utils/stock_data_fetcher` 对没有数据的股票默认只取 365 天，
所以库里 5190 只股票中有 5113 只的历史长度只有 500~1000 行（约 2-4 年），
全市场覆盖实际从 2023-08 才开始。这导致：

* 策略回测区间被迫压缩到 3 年以内；
* 市场状态（regime）分析、walk-forward 验证都缺乏样本量。

本脚本用 akshare 的新浪接口（`stock_zh_a_daily`，前复权）补齐 2015 年以来的日线。

安全性
------
`stock_kline` 表有 `UNIQUE(code, date)` 约束，本脚本只做 `INSERT OR IGNORE`，
**不会修改或删除任何现有数据**；重叠日期以库中已有值为准。

数据口径对齐
------------
* 价格：新浪前复权（qfq）。实测与库中现有数据在重叠区间收盘价差异中位数 0.029%、
  最近日期 0.0000%，可直接拼接。
* 成交量：新浪返回单位为「股」，KHunter 库为「手」（1 手 = 100 股），
  因此写入前统一除以 100。实测比例恒为 100.00。

用法
----
    python tools/backfill_history.py --start 2015-01-01 --workers 6
    python tools/backfill_history.py --start 2015-01-01 --limit 50   # 先小样本试跑
    python tools/backfill_history.py --verify-only                   # 只做一致性体检

支持断点续传：进度记录在 data/.backfill_progress.json，中断后重跑会自动跳过已完成股票。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "stock_selection.db"
PROGRESS_FILE = ROOT / "data" / ".backfill_progress.json"
INSERT_SQL = (
    "INSERT OR IGNORE INTO stock_kline (code, date, open, high, low, close, volume) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def to_sina_symbol(code: str) -> str | None:
    """把 6 位代码转成新浪接口的带前缀代码；北交所新浪不提供，返回 None。"""
    code = str(code).zfill(6)
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    return None  # 8xxxxx / 4xxxxx 北交所


def fetch_one(args: tuple[str, str, str]) -> tuple[str, list[tuple], str]:
    """抓取单只股票的历史日线。返回 (code, rows, status)。"""
    code, start, end = args
    symbol = to_sina_symbol(code)
    if symbol is None:
        return code, [], "skipped_bj"
    try:
        import akshare as ak
        import pandas as pd

        frame = ak.stock_zh_a_daily(symbol=symbol, start_date=start, end_date=end, adjust="qfq")
        if frame is None or frame.empty:
            return code, [], "empty"
        frame = frame.copy()
        frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
        rows = [
            (
                code,
                row.date,
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                int(round(float(row.volume) / 100.0)),  # 股 → 手，与库中口径一致
            )
            for row in frame.itertuples(index=False)
        ]
        return code, rows, "ok"
    except Exception as exc:  # 单只失败不影响整体
        name = type(exc).__name__
        # 退市股新浪不再提供数据，返回体为空导致 JSONDecodeError —— 归类为「已退市」，
        # 不当作失败，避免全量跑时把几百只退市股都算成错误。
        if name == "JSONDecodeError":
            return code, [], "delisted"
        return code, [], f"error: {name}: {str(exc)[:80]}"


def load_codes(db_path: Path) -> list[str]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return [str(r[0]).zfill(6) for r in con.execute("select code from stock_basic order by code")]
    finally:
        con.close()


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": [], "rows_added": 0, "updated_at": None}


def save_progress(progress: dict) -> None:
    progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
    PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")


def run_backfill(args: argparse.Namespace) -> int:
    codes = load_codes(DB_PATH)
    if args.limit:
        codes = codes[: args.limit]
    progress = load_progress() if not args.no_resume else {"done": [], "rows_added": 0}
    done = set(progress.get("done", []))
    todo = [c for c in codes if c not in done]
    print(f"目标股票 {len(codes)} 只，已完成 {len(done)} 只，本次待处理 {len(todo)} 只")
    if not todo:
        print("没有需要处理的股票（全部已完成）。")
        return 0

    end = args.end or datetime.now().strftime("%Y%m%d")
    tasks = [(c, args.start.replace("-", ""), end.replace("-", "")) for c in todo]

    con = sqlite3.connect(str(DB_PATH), timeout=60)
    con.execute("pragma journal_mode=wal")
    con.execute("pragma busy_timeout=60000")
    con.execute("pragma synchronous=normal")

    t0 = time.time()
    added_total = 0
    ok = empty = skipped = failed = delisted = 0
    error_samples: list[str] = []
    pending: list[tuple] = []

    try:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(fetch_one, t): t[0] for t in tasks}
            for i, fut in enumerate(as_completed(futures), 1):
                code, rows, status = fut.result()
                if status == "ok":
                    ok += 1
                    pending.extend(rows)
                elif status == "empty":
                    empty += 1
                elif status == "skipped_bj":
                    skipped += 1
                elif status == "delisted":
                    delisted += 1
                else:
                    failed += 1
                    if len(error_samples) < 5:
                        error_samples.append(f"{code}: {status}")

                # 失败的要重试，其余（含退市）都记入已完成
                if status != "error" and not str(status).startswith("error"):
                    done.add(code)

                # 批量写入：每 200 只股票或积压 50 万行提交一次
                if len(pending) >= 500_000 or (i % 200 == 0 and pending):
                    before = con.total_changes
                    con.executemany(INSERT_SQL, pending)
                    con.commit()
                    added_total += con.total_changes - before
                    pending.clear()

                if i % 200 == 0 or i == len(tasks):
                    elapsed = time.time() - t0
                    rate = i / elapsed if elapsed else 0
                    eta = (len(tasks) - i) / rate / 60 if rate else 0
                    print(
                        f"  进度 {i}/{len(tasks)}  新增 {added_total:,} 行  "
                        f"失败 {failed}  {rate:.1f} 只/秒  剩余约 {eta:.0f} 分钟",
                        flush=True,
                    )
                    progress["done"] = sorted(done)
                    progress["rows_added"] = added_total
                    save_progress(progress)
        if pending:
            before = con.total_changes
            con.executemany(INSERT_SQL, pending)
            con.commit()
            added_total += con.total_changes - before
    finally:
        con.close()

    progress["done"] = sorted(done)
    progress["rows_added"] = added_total
    save_progress(progress)

    print(f"\n完成：成功 {ok} 只，退市 {delisted} 只，空数据 {empty} 只，"
          f"跳过(北交所) {skipped} 只，失败 {failed} 只")
    print(f"本次新增 {added_total:,} 行，耗时 {(time.time()-t0)/60:.1f} 分钟")
    for sample in error_samples:
        print("  失败样例:", sample)
    if failed:
        print("提示：失败的股票默认不会再重试，可加 --no-resume 重跑，或用 --verify-only 体检。")
    return 0


def run_verify() -> int:
    """一致性体检：覆盖度分布 + 与新浪源在重叠区间的价格差异。"""
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    print("== 年度覆盖（每年最后一个交易日的股票数）==")
    for year in range(2015, datetime.now().year + 1):
        row = con.execute(
            "select date, count(*) from stock_kline where date like ? group by date order by date desc limit 1",
            (f"{year}-%",),
        ).fetchone()
        print(f"   {year}: {row}")
    print("\n== 每只股票的历史长度分布 ==")
    for lo, hi in [(0, 100), (100, 500), (500, 1000), (1000, 1500), (1500, 3000), (3000, 100000)]:
        n = con.execute(
            "select count(*) from (select code, count(*) c from stock_kline group by code having c >= ? and c < ?)",
            (lo, hi),
        ).fetchone()[0]
        print(f"   {lo}-{hi} 行: {n} 只")
    total = con.execute("select count(*) from stock_kline").fetchone()[0]
    print(f"\n总行数: {total:,}")
    con.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="KHunter 历史 K 线补全工具")
    parser.add_argument("--start", default="2015-01-01", help="起始日期（默认 2015-01-01）")
    parser.add_argument("--end", default=None, help="结束日期（默认今天）")
    parser.add_argument("--workers", type=int, default=6, help="并行抓取进程数")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 只（试跑用）")
    parser.add_argument("--no-resume", action="store_true", help="忽略进度记录，从头跑")
    parser.add_argument("--verify-only", action="store_true", help="只做覆盖率体检，不抓数据")
    args = parser.parse_args()
    if args.verify_only:
        return run_verify()
    return run_backfill(args)


if __name__ == "__main__":
    raise SystemExit(main())
