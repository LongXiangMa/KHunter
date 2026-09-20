"""命令行入口：vector_bt compare / validate / signal / list"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from vector_bt import __version__
from vector_bt.engine import BacktestConfig
from vector_bt.runner import compare, sweep
from vector_bt.signals import available_strategies
from vector_bt.validate import validate_strategy


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _print_ranking(table: pd.DataFrame) -> None:
    if table.empty:
        print("没有产生任何结果。")
        return
    cols = [
        "策略", "累计收益", "年化收益", "基准年化", "超额年化", "夏普",
        "信息比率", "最大回撤", "交易笔数", "逐笔胜率",
    ]
    cols = [c for c in cols if c in table.columns]
    view = table[cols].copy()
    for col in ("累计收益", "年化收益", "基准年化", "超额年化", "最大回撤", "逐笔胜率"):
        if col in view:
            view[col] = view[col].map(_pct)
    for col in ("夏普", "信息比率"):
        if col in view:
            view[col] = view[col].map(lambda v: f"{v:.2f}")
    print(view.to_string(index=True))


def _print_extra(label: str, table: pd.DataFrame) -> None:
    """补打一列次要指标，避免主表过宽。"""
    extra = [c for c in ("日胜率", "平均单笔", "盈亏比", "平均持有天数", "跟踪误差", "Beta", "Alpha") if c in table.columns]
    if not extra:
        return
    view = table[["策略"] + extra].copy()
    for col in ("日胜率", "平均单笔", "跟踪误差", "Alpha"):
        view[col] = view[col].map(_pct)
    view["盈亏比"] = view["盈亏比"].map(lambda v: "inf" if v == float("inf") else f"{v:.2f}")
    view["平均持有天数"] = view["平均持有天数"].map(lambda v: f"{v:.1f}")
    view["Beta"] = view["Beta"].map(lambda v: f"{v:.2f}")
    print(f"\n{label}")
    print(view.to_string(index=True))


def _add_regime_args(parser: argparse.ArgumentParser) -> None:
    """给子命令加上状态过滤相关参数。"""
    parser.add_argument(
        "--regime-filter",
        choices=["off", "rolling", "fixed"],
        default="off",
        help="状态过滤：rolling=滚动阈值（实盘可用）／fixed=固定阈值（对照实验）／off=不过滤",
    )
    parser.add_argument(
        "--regime-fixed",
        type=float,
        default=None,
        help="--regime-filter=fixed 时的阈值（小数，如 0.0155 表示 1.55%%）",
    )


def _build_regime_filter(args) -> "pd.Series | None":
    """按 CLI 参数构造"允许开仓"的日掩码。"""
    if getattr(args, "regime_filter", "off") == "off":
        return None
    from vector_bt.regime import build_regime_filter

    signal_start = args.signal_range[0] if getattr(args, "signal_range", None) else args.start
    mask, _ = build_regime_filter(
        None,
        market_start=signal_start,
        market_end=args.end,
        method=args.regime_filter,
        fixed_threshold=args.regime_fixed,
        db_path=args.db,
    )
    return mask


def cmd_list(args: argparse.Namespace) -> int:
    names = available_strategies(args.khunter_root)
    print(f"khunter-vectorbt {__version__} —— 可用策略 {len(names)} 个：")
    for name in names:
        print("  -", name)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    names = available_strategies(args.khunter_root) if args.strategy == "all" else [args.strategy]
    rows = []
    for name in names:
        result = validate_strategy(
            name,
            start=args.start,
            end=args.end,
            stocks=args.stocks,
            dates_per_stock=args.dates,
            db_path=args.db,
            khunter_root=args.khunter_root,
            window=args.window,
            min_history=args.min_history,
        )
        rows.append(result)
        flag = "OK " if result["一致率"] == 1.0 else "差异"
        print(
            f"[{flag}] {name:<16} 一致 {result['一致数']}/{result['检查数']} "
            f"异常 {result['策略异常']}"
        )
        for diff in result["差异样本"][:3]:
            print(f"        {diff}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"已写出 {args.json}")
    bad = [r for r in rows if r["一致率"] < 1.0]
    return 1 if bad else 0


def cmd_signal(args: argparse.Namespace) -> int:
    from vector_bt import data
    from vector_bt.runner import generate_signals

    codes = data.load_symbols(db_path=args.db, start=args.start, end=args.end, min_rows=args.min_history)
    code_list = list(codes)[: args.top_n] if args.top_n else list(codes)
    frames = generate_signals(
        [args.strategy],
        code_list,
        args.start,
        args.end,
        db_path=args.db,
        khunter_root=args.khunter_root,
        jobs=args.jobs,
        window=args.window,
        min_history=args.min_history,
    )
    frame = frames[args.strategy]
    if frame.empty:
        print("没有信号。")
        return 0
    out = Path(args.out or f"signals_{args.start}_{args.end}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out, encoding="utf-8-sig")
    print(f"{frame.shape[0]} 天 × {frame.shape[1]} 只股票，信号数 {int(frame.notna().sum().sum())}")
    print(f"已写出 {out}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    # --strategy 是 nargs="+"，所以 "all" 会以 ['all'] 的形式传进来
    use_all = len(args.strategy) == 1 and str(args.strategy[0]).lower() == "all"
    names = available_strategies(args.khunter_root) if use_all else args.strategy
    config = BacktestConfig(
        initial_capital=args.capital,
        hold_period=args.hold_period,
        max_daily_buys=args.max_daily_buys,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit,
    )
    benchmark = None if args.no_benchmark else args.benchmark
    signal_start = signal_end = None
    if args.signal_range:
        signal_start, signal_end = args.signal_range
    regime_mask = _build_regime_filter(args)
    table, results, _ = compare(
        names,
        start=args.start,
        end=args.end,
        signal_start=signal_start,
        signal_end=signal_end,
        top_n=args.top_n,
        db_path=args.db,
        khunter_root=args.khunter_root,
        jobs=args.jobs,
        window=args.window,
        min_history=args.min_history,
        config=config,
        benchmark=benchmark,
        regime_filter=regime_mask,
        save=bool(getattr(args, "save", False)),
        backtest_name=getattr(args, "backtest_name", None),
        use_cache=not args.no_cache,
    )
    print()
    _print_ranking(table)
    _print_extra("其他指标", table)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n排名表已写出 {out}")
        trades_dir = out.with_suffix("")
        trades_dir.mkdir(exist_ok=True)
        for name, result in results.items():
            if not result.trades_frame.empty:
                safe = name.replace("/", "_")
                result.trades_frame.to_csv(
                    trades_dir / f"trades_{safe}.csv", index=False, encoding="utf-8-sig"
                )
            if not result.equity_curve.empty:
                safe = name.replace("/", "_")
                curve = pd.DataFrame({"策略净值": result.equity_curve})
                if not result.benchmark_curve.empty:
                    curve["基准净值"] = result.benchmark_curve.reindex(curve.index)
                curve.to_csv(trades_dir / f"equity_{safe}.csv", encoding="utf-8-sig")
        print(f"逐笔明细已写出 {trades_dir}/")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    names = available_strategies(args.khunter_root) if args.strategy == ["all"] else args.strategy
    regime_mask = _build_regime_filter(args)
    table = sweep(
        names,
        start=args.start,
        end=args.end,
        stop_losses=[v / 100 for v in args.stop_loss],
        take_profits=[v / 100 for v in args.take_profit],
        hold_periods=args.hold_period,
        db_path=args.db,
        khunter_root=args.khunter_root,
        jobs=args.jobs,
        window=args.window,
        min_history=args.min_history,
        benchmark=None if args.no_benchmark else args.benchmark,
        regime_filter=regime_mask,
        use_cache=not args.no_cache,
    )
    if table.empty:
        print("没有结果。")
        return 0
    view = table[
        ["策略", "止损", "止盈", "持有期", "年化收益", "超额年化", "信息比率", "夏普", "最大回撤", "交易笔数"]
    ].copy()
    view["止损"] = (view["止损"] * 100).map(lambda v: f"{v:.0f}%")
    view["止盈"] = (view["止盈"] * 100).map(lambda v: f"{v:.0f}%")
    for col in ("年化收益", "超额年化", "最大回撤"):
        view[col] = view[col].map(_pct)
    for col in ("信息比率", "夏普"):
        view[col] = view[col].map(lambda v: f"{v:.2f}")
    print(f"\n参数扫描结果（按超额年化排序，共 {len(table)} 个组合）\n")
    print(view.head(args.show).to_string(index=False))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n完整结果已写出 {out}")
        print("\n各策略最优组合：")
        best = table.loc[table.groupby("策略")["超额年化"].idxmax()]
        for _, r in best.sort_values("超额年化", ascending=False).iterrows():
            print(
                "  {:<16} 止损{:>4.0f}% 止盈{:>4.0f}% 持有{:>3}日 → 超额 {:+.2%}  IR {:.2f}  回撤 {:.2%}  交易 {}".format(
                    r["策略"], r["止损"] * 100, r["止盈"] * 100, int(r["持有期"]),
                    r["超额年化"], r["信息比率"], r["最大回撤"], int(r["交易笔数"]),
                )
            )
    return 0


def cmd_regime(args: argparse.Namespace) -> int:
    from vector_bt.benchmark import load_benchmark
    from vector_bt.regime import (
        classify,
        load_market_breadth,
        market_features,
        regime_performance,
    )

    index_close = load_benchmark(args.benchmark, args.start, args.end)
    breadth = load_market_breadth(args.start, args.end, db_path=args.db)
    features = market_features(index_close, breadth)
    regimes = classify(features)

    print(f"区间 {args.start} ~ {args.end}，共 {len(regimes)} 个交易日")
    print("\n状态分布：")
    dist = regimes["regime"].value_counts()
    for name, count in dist.items():
        print(f"  {name:<12} {count:>4} 天  ({count / len(regimes):.1%})")
    print(f"\n活跃度分档阈值（涨停家数占比 5 日均值中位数）: {regimes['activity_threshold'].iloc[0]:.2%}")

    table = regime_performance(args.run_dir, regimes)
    if table.empty:
        print("没有可统计的结果。")
        return 1

    pivot = table.pivot(index="策略", columns="状态", values="年化超额")
    order = [c for c in ["上行·高活跃", "上行·低活跃", "震荡·高活跃", "震荡·低活跃", "下行·高活跃", "下行·低活跃"] if c in pivot.columns]
    pivot = pivot[order]
    view = pivot.map(lambda v: f"{v:+.1%}" if pd.notna(v) else "—")
    print("\n各状态下的年化超额：")
    print(view.to_string())

    ir_pivot = table.pivot(index="策略", columns="状态", values="信息比率")[order]
    print("\n各状态下的信息比率：")
    print(ir_pivot.round(2).to_string())

    print("\n每个策略的最优状态：")
    for name, group in table.groupby("策略"):
        best = group.loc[group["年化超额"].idxmax()]
        positive = group[group["年化超额"] > 0]
        pos_states = "、".join(positive["状态"].tolist()) or "无"
        print(
            "  {:<16} 最优 {:<12} {:+.1%}  正超额状态: {}".format(
                name, best["状态"], best["年化超额"], pos_states
            )
        )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(out, index=False, encoding="utf-8-sig")
        regimes.to_csv(out.with_name("regime_daily.csv"), encoding="utf-8-sig")
        print(f"\n明细已写出 {out} 与 {out.with_name('regime_daily.csv')}")
    return 0


def cmd_regime_today(args: argparse.Namespace) -> int:
    """每日实盘决策：输出当前活跃度是否达标，以及建议启用的策略。"""
    from vector_bt import data
    from vector_bt.regime import daily_regime_status

    end = args.end or data.load_calendar(start=None, end=None, db_path=args.db)[-1]
    status = daily_regime_status(
        end=end, window=args.window, benchmark=args.benchmark, db_path=args.db
    )
    print(f"交易日        : {status['date']}")
    print(f"沪深300 收盘  : {status['index_close']:.2f}   20日涨跌: {status['ret_20d']:+.2%}")
    print(f"涨停家数占比  : {status['limit_up_ma']:.3%}（5 日均值）")
    print(f"滚动阈值      : {status['threshold']:.3%}（过去 {args.window} 日中位数）")
    print()
    if status["active"]:
        print("状态          : [开仓] 高活跃 —— 允许开新仓")
        print("建议启用      : 涨停横盘策略（11 年样本外验证：IR 1.01，回撤 -37.5%）")
        print("暂停          : 其余策略在 11 年区间均为负超额，不建议启用")
    else:
        print("状态          : [空仓] 低活跃 —— 建议空仓或暂停开新仓")
        print("依据          : 涨停横盘在高活跃状态年化超额 +27.5%，低活跃状态仅 -3.8%")
    print()
    print("提示：本判断只用到当日及之前的市场数据，可直接用于次日决策。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vector_bt", description="KHunter 策略的向量化对比回测工具"
    )
    parser.add_argument("--version", action="version", version=f"khunter-vectorbt {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--start", required=True, help="回测开始日期 YYYY-MM-DD")
        p.add_argument("--end", required=True, help="回测结束日期 YYYY-MM-DD")
        p.add_argument("--db", default=None, help="KHunter SQLite 路径")
        p.add_argument("--khunter-root", default=None, help="KHunter 项目根目录")
        p.add_argument("--window", type=int, default=180, help="策略评估窗口（K 线根数）")
        p.add_argument("--min-history", type=int, default=60, help="最少历史根数")
        p.add_argument("--jobs", type=int, default=1, help="并行进程数")

    p_list = sub.add_parser("list", help="列出可用策略")
    p_list.add_argument("--khunter-root", default=None)
    p_list.set_defaults(func=cmd_list)

    p_val = sub.add_parser("validate", help="保真校验：优化路径 vs 原始实现")
    p_val.add_argument("--strategy", default="all")
    p_val.add_argument("--stocks", type=int, default=5)
    p_val.add_argument("--dates", type=int, default=10)
    p_val.add_argument("--json", default=None)
    add_common(p_val)
    p_val.set_defaults(func=cmd_validate)

    p_sig = sub.add_parser("signal", help="生成单个策略的信号矩阵")
    p_sig.add_argument("--strategy", required=True)
    p_sig.add_argument("--top-n", type=int, default=None)
    p_sig.add_argument("--out", default=None)
    add_common(p_sig)
    p_sig.set_defaults(func=cmd_signal)

    p_cmp = sub.add_parser("compare", help="跑一轮策略对比并输出排名")
    p_cmp.add_argument("--strategy", nargs="+", default=["all"])
    p_cmp.add_argument("--top-n", type=int, default=None, help="只用前 N 只股票（调试用）")
    p_cmp.add_argument("--capital", type=float, default=300_000.0)
    p_cmp.add_argument("--hold-period", type=int, default=10)
    p_cmp.add_argument("--max-daily-buys", type=int, default=8)
    p_cmp.add_argument("--stop-loss", type=float, default=-0.07)
    p_cmp.add_argument("--take-profit", type=float, default=0.21)
    p_cmp.add_argument("--benchmark", default="000300", help="基准指数代码（默认沪深300）")
    p_cmp.add_argument("--no-benchmark", action="store_true", help="关闭基准对比")
    _add_regime_args(p_cmp)
    p_cmp.add_argument("--no-cache", action="store_true", help="忽略信号缓存，强制重算")
    p_cmp.add_argument(
        "--signal-range",
        nargs=2,
        metavar=("START", "END"),
        default=None,
        help="信号计算区间（默认同回测区间）。多区间验证时设成宽区间，信号只算一次",
    )
    p_cmp.add_argument("--out", default=None, help="排名表输出 CSV 路径")
    p_cmp.add_argument("--save", action="store_true",
                       help="把结果写入 KHunter 的 backtest_result/backtest_trade（Web 端「回测历史」可见）")
    p_cmp.add_argument("--backtest-name", default=None, help="写入回测记录时使用的名称")
    add_common(p_cmp)
    p_cmp.set_defaults(func=cmd_compare)

    p_sw = sub.add_parser("sweep", help="参数扫描：固定信号，只变止损/止盈/持有期")
    p_sw.add_argument("--strategy", nargs="+", default=["all"])
    p_sw.add_argument("--stop-loss", type=float, nargs="+", default=[-5, -7, -10, -15],
                      help="止损百分比，如 -5 -7 -10（默认 -5 -7 -10 -15）")
    p_sw.add_argument("--take-profit", type=float, nargs="+", default=[10, 15, 21, 30],
                      help="止盈百分比，如 10 15 21（默认 10 15 21 30）")
    p_sw.add_argument("--hold-period", type=int, nargs="+", default=[5, 10, 20],
                      help="持有交易日上限（默认 5 10 20）")
    p_sw.add_argument("--show", type=int, default=20, help="控制台显示前 N 行")
    p_sw.add_argument("--benchmark", default="000300")
    p_sw.add_argument("--no-benchmark", action="store_true")
    _add_regime_args(p_sw)
    p_sw.add_argument("--no-cache", action="store_true")
    p_sw.add_argument("--out", default=None)
    add_common(p_sw)
    p_sw.set_defaults(func=cmd_sweep)

    p_rg = sub.add_parser("regime", help="市场状态分层：看策略在什么行情下有效")
    p_rg.add_argument("--run-dir", required=True, help="含 equity_*.csv 的回测输出目录")
    p_rg.add_argument("--benchmark", default="000300")
    p_rg.add_argument("--out", default=None)
    add_common(p_rg)
    p_rg.set_defaults(func=cmd_regime)

    p_today = sub.add_parser("regime-today", help="输出最新交易日的活跃度状态与开仓建议")
    p_today.add_argument("--end", default=None, help="指定交易日（默认取库中最新）")
    p_today.add_argument("--window", type=int, default=250, help="滚动阈值窗口（默认 250 日）")
    p_today.add_argument("--benchmark", default="000300")
    p_today.add_argument("--db", default=None)
    p_today.set_defaults(func=cmd_regime_today)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
