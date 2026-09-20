# KHunter v1.9.0 发版说明

- **版本号**：v1.9.0
- **发布日期**：2026-09-21
- **发布类型**：功能新增 —— 向量化回测结果写入既有回测记录

---

## 一、为什么做这次迭代

用户反馈：「之前的策略胜率什么的没有更新进去」。

排查发现根因：KHunter 原有的事件驱动回测**从来没有完整跑完过**，
`backtest_result` / `backtest_trade` / `backtest_config` 三张表都是 0 行，
所以：

- 「策略回测」页面看不到任何结果
- 「回测历史」页面是空的
- 之前那些策略评估结论（11 年超额、逐笔胜率、信息比率）只存在于
  报告文件和对话里，软件里体现不出来

本次迭代把量化验证模块的回测结果**写入这两张既有表**，让历史结果在原有页面可见。

> 这是「新增能力必须落到 KHunter 上」这条规则的第一次实践 ——
> 能力做完了，但没接到用户实际会看的界面上，等于没做。

---

## 二、改了什么

### 1. 新增 `vector_bt/persistence.py`

把 `BacktestResult` 写入 `backtest_result`（1 行汇总）+ `backtest_trade`（逐笔明细）。

**字段单位与原有引擎严格对齐**（这是接进既有表最容易错的地方）：

| 字段 | 单位 | 依据 |
|---|---|---|
| `win_rate` | 百分数（42.56 表示 42.56%） | 原引擎 `win_rate = win/total*100`；前端直接加 `%` 显示 |
| `total_return` | 百分数 | 前端用 `initial_capital*(1+total_return/100)` 反算终值 |
| `max_drawdown` | **正数**百分数 | 原引擎用 `abs(min(drawdown))` |
| `sell_type` | `stop_loss`/`take_profit`/`hold_expired` | 原引擎枚举值 |

逐笔明细的买入金额为等权近似（`initial_capital / max_daily_buys`，数量按 100 股整手向下取整），
与原引擎的真实资金占用口径不同，因此回测名称统一带「向量化回测」前缀以便区分。

### 2. 接口与命令行

- `POST /api/vector/backtest` 新增 `save` 与 `backtest_name` 字段
- `python -m vector_bt compare ... --save --backtest-name "..."` 

### 3. 界面

「量化验证」页面的「开始回测」按钮旁新增 **☑ 写入「回测历史」**（默认勾选），
回测完成后自动出现在左侧「回测管理 → 回测历史」。

---

## 三、实测

```powershell
cd D:\chatgpt\KHunter
python -m vector_bt compare --strategy "涨停横盘策略" `
    --start 2021-01-04 --end 2026-09-18 `
    --signal-range 2015-01-01 2026-09-18 --window 120 `
    --regime-filter rolling --save --backtest-name "向量化回测 涨停横盘 2021-2026（滚动过滤）"
```

执行后：

| 检查项 | 结果 |
|---|---|
| `backtest_result` | 1 行，胜率 42.25%、累计 107.91%、回撤 53.14% |
| `backtest_trade` | **1136 行**逐笔明细（含止损/止盈/到期标注） |
| `GET /api/trading/backtest/results` | 返回该记录 → 「回测历史」页可见 |

---

## 四、已知限制

- 逐笔买入金额是等权近似，**不等同于**原引擎的资金占用模拟；用于横向对比足够，
  用于精确资金曲线请以原引擎结果为准
- 已写入的记录与原有引擎记录混在同一张表，靠 `backtest_name` 前缀区分
  （无数据库级字段标识，未来如需要可加 `source` 列）
