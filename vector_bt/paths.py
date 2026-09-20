"""路径常量：所有默认路径都以 KHunter 根目录为基准，避免写死绝对路径。"""

from __future__ import annotations

from pathlib import Path

# vector_bt/ 的上一级就是 KHunter 根目录
ROOT: Path = Path(__file__).resolve().parents[1]

DB_PATH: Path = ROOT / "data" / "stock_selection.db"

# 回测缓存（信号矩阵 / 基准序列 / 市场宽度）属于可重建的派生产物，
# 放在 data/ 下并与业务库隔离，已在 .gitignore 中排除。
CACHE_DIR: Path = ROOT / "data" / "vector_bt_cache"
