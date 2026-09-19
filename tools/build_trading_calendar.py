"""交易日历缓存生成工具（KHunter v1.7.0 新增）

解决什么问题
------------
`utils/trade_date_utils.py` 的 `is_trading_day()` / `get_trading_days()` 依赖：

1. 本地缓存 `data/trading_calendar_cache.json`，或
2. Tushare（需要 `config/tushare_config.json` 里的 token）

两者都没有时会直接抛错，后果是：

* 选股时"非交易日自动回退到上一个交易日"失效，回退到原日期 → 当天无 K 线 → 0 只股票；
* 股票池、回测里任何用到交易日的地方同样失败。

本脚本用 akshare 的新浪交易日历接口生成该缓存，**无需 Tushare token**，
让系统在完全离线（无 token）的情况下也能正常判断交易日。

用法
----
    python tools/build_trading_calendar.py
    python tools/build_trading_calendar.py --check   # 只检查缓存是否存在与覆盖范围
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_FILE = ROOT / "data" / "trading_calendar_cache.json"


def build() -> int:
    try:
        import akshare as ak
    except ImportError:
        print("缺少 akshare，请先执行: pip install akshare")
        return 1

    print("正在从新浪获取 A 股交易日历...")
    frame = ak.tool_trade_date_hist_sina()
    if frame is None or frame.empty:
        print("获取失败：返回为空")
        return 1

    dates = sorted({str(d) for d in frame["trade_date"]})
    # 与 trade_date_utils._update_cache_from_tushare 写出的格式保持一致
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps({"dates": dates}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已写出 {CACHE_FILE}")
    print(f"  交易日 {len(dates)} 天，范围 {dates[0]} ~ {dates[-1]}")
    print(f"  文件大小 {CACHE_FILE.stat().st_size / 1024:.1f} KB")
    print("\n提示：该文件已被 .gitignore 忽略（属于运行时缓存）。"
          "换新环境或跨年后重新执行本脚本即可。")
    return 0


def check() -> int:
    if not CACHE_FILE.exists():
        print(f"缓存不存在: {CACHE_FILE}")
        print("请执行 python tools/build_trading_calendar.py 生成。")
        return 1
    data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    dates = data.get("dates", [])
    print(f"缓存存在: {CACHE_FILE}")
    print(f"  交易日 {len(dates)} 天，范围 {dates[0]} ~ {dates[-1]}")
    today = datetime.now().strftime("%Y-%m-%d")
    if dates and dates[-1] < today:
        print(f"  ⚠️ 缓存最后日期 {dates[-1]} 早于今天 {today}，建议重新生成。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 KHunter 交易日历缓存")
    parser.add_argument("--check", action="store_true", help="只检查缓存状态，不重新生成")
    args = parser.parse_args()
    return check() if args.check else build()


if __name__ == "__main__":
    raise SystemExit(main())
