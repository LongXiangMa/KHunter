# KHunter 项目约定

本文件由 Codex 在本目录开始任务时自动读取。通用习惯见 `D:\chatgpt\AGENTS.md`。

## 项目现状

KHunter 是 A 股量化交易系统（Flask Web + SQLite），本仓库是**用户自己的 fork**：

| remote | 地址 | 权限 |
| --- | --- | --- |
| `origin` | `git@github.com:LongXiangMa/KHunter.git` | 可读写，推送目标 |
| `upstream` | `git@github.com:ling-0729/KHunter.git` | **无推送权限，不要尝试推送** |

## 目录要点

| 路径 | 说明 |
| --- | --- |
| `web_server.py` | 主入口，注册各蓝图（trading / khunter / vector_bt 等） |
| `strategy/` | 19 个选股策略，每个一个文件，继承 `BaseStrategy` |
| `utils/` | 数据抓取、DB、交易日历等工具 |
| `data/stock_selection.db` | 主数据库（1100 万行 K 线，2015-2026） |
| `data/vector_bt_cache/` | 量化验证模块的缓存（约 300 MB，**勿入库**） |
| `vector_bt/` | **量化验证模块（主实现）**，见下 |
| `tools/` | 独立脚本：历史数据补全、交易日历生成 |

## 量化验证模块（vector_bt）

2026-09-21 起的约定：**新增的分析/回测能力都要落在这里，不要只放在外部脚本**。

- 能力：向量化回测（快，用于横向对比策略）、市场状态开关（活跃度择时）、参数扫描
- 入口：Web 界面「回测管理 → 🧪 量化验证」，或 API `/api/vector/...`，或命令行
  `python -m vector_bt regime-today`
- 引擎与原有事件驱动回测**不等价**：本模块用日线收盘价 + 等权持有近似，
  适合多策略横向比较；精细评估单策略请用原有回测引擎
- 首次跑某个新区间需要生成信号（较慢），之后命中缓存只需数十秒

## 关键结论（勿重复推翻）

- 19 个形态策略在 11 年（2015-2026）维度上只有「涨停横盘策略」具备正超额
  （年化 8.76%、超额 +6.92%），其余 4 个为爆仓级亏损（累计 -76% ~ -99.5%）
- **活跃度开关有效**：高活跃状态年化超额 +27.5%，低活跃 -3.8%；
  样本外（2021-2026）验证通过：年化 11.45% → 24.42%，回撤 -65.3% → -37.5%
- 详情见 `RELEASE_NOTES_v1.8.0.md` 与 `D:\chatgpt\tools\khunter-vectorbt\reports\策略评估总结.md`

## 改动后自测

```powershell
# 1) 引擎单测（改了 vector_bt/engine.py 必跑）
python -m pytest D:\chatgpt\tools\khunter-vectorbt\tests -q

# 2) 命令行自测（不依赖 Web 服务）
cd D:\chatgpt\KHunter; python -m vector_bt regime-today

# 3) 重启服务（改了 web_server.py / index.html 必须重启）
#    启动后检查 logs\app.<日期>.log 出现「已注册xxx蓝图」且无 ERROR

# 4) 接口冒烟
Invoke-RestMethod http://127.0.0.1:5001/api/vector/regime
Invoke-RestMethod http://127.0.0.1:5001/api/vector/strategies
```

## 版本与发布

- 功能新增 → `vX.Y.0`；修复 → `vX.Y.Z+1`
- 发版说明写到仓库根目录 `RELEASE_NOTES_vX.Y.Z.md`
- 发布流程：自测 → `git commit` → `git tag -a vX.Y.Z` → `git push origin main --tags`
- 未经用户明确要求，不重写历史、不 force push

## 数据与缓存红线

- **新增任何缓存目录，第一件事是补 `.gitignore`**（曾因漏写把 290 MB 缓存推送进
  khunter-vectorbt 仓库）
- 数据库 `data/*.db`、缓存 `data/vector_bt_cache/`、`.venv/`、`.obsidian/` 均不入库
