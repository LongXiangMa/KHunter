"""保真校验：把"指标预计算 + 切片"的优化路径与原始实现逐条对拍。

这是本项目最重要的测试。策略逻辑没有重写，但"指标只在整段历史上算一次"
是一个**假设**——只有当 `calculate_indicators` 是因果的（第 i 行仅依赖 <= i 的数据）
才成立。若某个策略用了非因果统计（例如对整段做标准化），校验会报出差异。
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Sequence

import pandas as pd

from vector_bt import data
from vector_bt.signals import StrategyAdapter


def validate_strategy(
    strategy: str,
    *,
    start: str,
    end: str,
    stocks: int = 5,
    dates_per_stock: int = 10,
    seed: int = 20260920,
    db_path: str | Path | None = None,
    khunter_root: str | Path | None = None,
    window: int = 180,
    min_history: int = 60,
) -> dict:
    """返回 {策略, 检查数, 一致数, 一致率, 差异样本}。"""
    codes = data.load_symbols(db_path=db_path, start=start, end=end, min_rows=min_history)
    picked = list(codes)[: max(1, stocks)]
    klines = data.load_klines(db_path=db_path, codes=picked, start=start, end=end)
    adapter = StrategyAdapter(
        strategy, khunter_root=khunter_root, window=window, min_history=min_history
    )

    rng = random.Random(seed)
    checked = agreed = 0
    diffs: list[dict] = []

    for code, df in klines.items():
        dates = [str(v)[:10] for v in df["date"].tolist()]
        if len(dates) <= min_history + 5:
            continue
        targets = [dates[i] for i in rng.sample(range(min_history, len(dates)), min(dates_per_stock, len(dates) - min_history))]
        indicators = adapter.prepare(df)
        fast = adapter.scan(indicators, code, codes[code], only_dates=targets, raw_frame=df)
        reference = adapter.scan_reference(df, code, codes[code], dates=targets)
        for date in targets:
            f, r = fast.get(date), reference.get(date)
            checked += 1
            same = (f is None and r is None) or (
                f is not None and r is not None and abs(f - r) < 1e-9
            )
            if same:
                agreed += 1
            elif len(diffs) < 10:
                diffs.append({"code": code, "date": date, "fast": f, "reference": r})

    return {
        "策略": strategy,
        "检查数": checked,
        "一致数": agreed,
        "一致率": (agreed / checked) if checked else 0.0,
        "差异样本": diffs,
        "策略异常": adapter.errors,
    }
