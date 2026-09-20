"""调度层：并行生成信号矩阵，并驱动向量化回测。"""

from __future__ import annotations

import multiprocessing as mp
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from vector_bt import data
from vector_bt.benchmark import DEFAULT_BENCHMARK, DEFAULT_CACHE_DIR, load_benchmark
from vector_bt.engine import BacktestConfig, BacktestResult, run_backtest
from vector_bt.signals import StrategyAdapter


def price_frames(
    codes: Sequence[str],
    start: str,
    end: str,
    db_path: str | Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """返回 (close, high, low) 三个宽表，index=日期，columns=股票代码。"""
    klines = data.load_klines(db_path=db_path, codes=codes, start=start, end=end)
    if not klines:
        raise ValueError("价格数据为空")
    close = pd.DataFrame({c: df.set_index("date")["close"] for c, df in klines.items()})
    high = pd.DataFrame({c: df.set_index("date")["high"] for c, df in klines.items()})
    low = pd.DataFrame({c: df.set_index("date")["low"] for c, df in klines.items()})
    close.index = pd.to_datetime(close.index)
    high.index = pd.to_datetime(high.index)
    low.index = pd.to_datetime(low.index)
    return close.sort_index(), high.sort_index(), low.sort_index()


@dataclass
class SignalOutcome:
    """一个 worker 的产出。"""

    strategy: str
    records: list[tuple[str, str, float]]  # (date, code, score)
    errors: int
    unsupported_ratio: float


def _worker(payload: dict) -> SignalOutcome:
    """子进程入口：给定股票分片和策略，产出信号记录。"""
    codes: list[str] = payload["codes"]
    start: str = payload["start"]
    end: str = payload["end"]
    strategy_name: str = payload["strategy"]
    db_path = payload["db_path"]
    window: int = payload["window"]
    min_history: int = payload["min_history"]

    names = data.load_stock_names(db_path)
    klines = data.load_klines(db_path=db_path, codes=codes, start=start, end=end)
    adapter = StrategyAdapter(
        strategy_name, khunter_root=payload["khunter_root"], window=window, min_history=min_history
    )

    records: list[tuple[str, str, float]] = []
    failed_codes = 0
    for code, df in klines.items():
        if df.empty:
            continue
        try:
            indicators = adapter.prepare(df)
            signals = adapter.scan(indicators, code, names.get(code, ""), raw_frame=df)
        except Exception:
            failed_codes += 1
            continue
        for date, score in signals.items():
            records.append((date, code, float(score)))

    total_codes = max(1, len(klines))
    return SignalOutcome(
        strategy=strategy_name,
        records=records,
        errors=adapter.errors + failed_codes,
        unsupported_ratio=failed_codes / total_codes,
    )


def _universe_digest(codes: Sequence[str]) -> str:
    """股票池指纹：把池子内容算进缓存键，避免"小样本跑出的缓存被全量跑误用"。"""
    import hashlib

    joined = ",".join(sorted(str(c) for c in codes))
    return hashlib.md5(joined.encode("utf-8")).hexdigest()[:10]


def _signal_cache_path(
    cache_dir: Path,
    strategy: str,
    start: str,
    end: str,
    codes: Sequence[str],
    window: int,
    min_history: int,
) -> Path:
    safe = str(strategy).replace("/", "_").replace("\\", "_")
    # 缓存键必须包含：策略 + 区间 + 股票池 + 窗口参数。
    # 少任何一项都可能复用错误的信号（踩过：400 只的样本缓存污染全量结果）。
    digest = _universe_digest(codes)
    return Path(cache_dir) / (
        f"signals_{safe}_{start}_{end}_n{len(codes)}_w{window}_h{min_history}_{digest}.pkl"
    )


def generate_signals(
    strategies: Sequence[str],
    codes: Sequence[str],
    start: str,
    end: str,
    *,
    db_path: str | Path | None = None,
    khunter_root: str | Path | None = None,
    jobs: int = 1,
    window: int = 180,
    min_history: int = 60,
    use_cache: bool = True,
    cache_dir: Path | str | None = None,
    top_n: int | None = None,
    verbose: bool = True,
) -> dict[str, pd.DataFrame]:
    """并行生成 {策略: 信号宽表}。jobs>1 时按"策略 × 股票分片"派发。

    信号生成是整个流程最贵的一步（19 策略 × 5000 只 × 1 年约 30 分钟），
    因此结果默认落盘缓存（``.cache/signals_<策略>_<区间>.pkl``），
    重复回测（例如只改基准或交易参数）时可直接复用。
    """
    from vector_bt.signals import DEFAULT_KHUNTER_ROOT

    root = str(khunter_root or DEFAULT_KHUNTER_ROOT)
    db = str(db_path) if db_path else None
    jobs = max(1, int(jobs))
    codes = list(codes)

    cache_root = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    if use_cache:
        cache_root.mkdir(parents=True, exist_ok=True)

    frames: dict[str, pd.DataFrame] = {}
    pending: list[str] = []
    for name in strategies:
        path = _signal_cache_path(cache_root, name, start, end, codes, window, min_history)
        if use_cache and path.exists():
            try:
                frames[name] = pd.read_pickle(path)
                if verbose:
                    print(f"  · {name} 命中信号缓存")
                continue
            except Exception:
                pass
        pending.append(name)

    if not pending:
        return frames

    # 分片数刻意多于进程数：每个 worker 依次处理多个小分片，单次驻留内存更小。
    # 11 年区间（2700 交易日 × 4700 只）实测过：分片过大 + 结果全量累积会把内存打爆，
    # 进程被系统杀掉且不留日志。
    n_chunks = max(jobs, 24)
    chunks = data.chunked(codes, n_chunks)

    tasks = [
        {
            "codes": chunk,
            "start": start,
            "end": end,
            "strategy": name,
            "db_path": db,
            "khunter_root": root,
            "window": window,
            "min_history": min_history,
        }
        for name in pending
        for chunk in chunks
        if chunk
    ]

    started = time.time()
    # 按策略聚合：某个策略的全部分片都回来后立刻落盘缓存并释放记录，
    # 避免把 6 个策略 × 4700 只股票的信号全压在内存里。
    records_by_strategy: dict[str, list[tuple[str, str, float]]] = {n: [] for n in pending}
    tasks_by_strategy: dict[str, int] = {n: 0 for n in pending}
    for task in tasks:
        tasks_by_strategy[task["strategy"]] += 1
    frames_ready: dict[str, pd.DataFrame] = {}

    def _absorb(outcome: SignalOutcome) -> None:
        bucket = records_by_strategy.get(outcome.strategy)
        if bucket is None:
            return
        bucket.extend(outcome.records)
        tasks_by_strategy[outcome.strategy] -= 1
        if tasks_by_strategy[outcome.strategy] > 0:
            return
        rows = bucket
        records_by_strategy[outcome.strategy] = []  # 立刻释放
        if not rows:
            frames_ready[outcome.strategy] = pd.DataFrame()
            return
        frame = pd.DataFrame(rows, columns=["date", "code", "score"])
        wide = frame.pivot_table(index="date", columns="code", values="score", aggfunc="max")
        wide.index = pd.to_datetime(wide.index)
        wide = wide.sort_index()
        if top_n:
            wide = wide.iloc[:, :top_n]
        frames_ready[outcome.strategy] = wide
        if use_cache:
            try:
                wide.to_pickle(
                    _signal_cache_path(cache_root, outcome.strategy, start, end, codes, window, min_history)
                )
                if verbose:
                    print(f"  ✓ {outcome.strategy} 信号已落盘缓存 {wide.shape}", flush=True)
            except Exception:
                pass

    if jobs == 1 or len(tasks) == 1:
        for task in tasks:
            _absorb(_worker(task))
            if verbose:
                print(f"  · {task['strategy']} 分片完成 {len(task['codes'])} 只", flush=True)
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=min(jobs, len(tasks))) as pool:
            for outcome in pool.imap_unordered(_worker, tasks):
                _absorb(outcome)
                if verbose:
                    print(f"  · {outcome.strategy} 分片完成（{len(outcome.records)} 个信号）", flush=True)
    if verbose:
        print(f"  信号生成总耗时 {time.time() - started:.1f}s，任务数 {len(tasks)}")

    frames.update(frames_ready)
    return frames


def compare(
    strategies: Sequence[str],
    *,
    start: str,
    end: str,
    signal_start: str | None = None,
    signal_end: str | None = None,
    top_n: int | None = None,
    db_path: str | Path | None = None,
    khunter_root: str | Path | None = None,
    jobs: int = 1,
    window: int = 180,
    min_history: int = 60,
    config: BacktestConfig | None = None,
    benchmark: str | None = DEFAULT_BENCHMARK,
    regime_filter: pd.Series | None = None,
    save: bool = False,
    backtest_name: str | None = None,
    use_cache: bool = True,
    cache_dir: Path | str | None = None,
    verbose: bool = True,
) -> tuple[pd.DataFrame, dict[str, BacktestResult], dict[str, pd.DataFrame]]:
    """跑一轮策略对比，返回 (排名表, 明细结果, 信号矩阵)。

    signal_start/signal_end：信号的计算区间。默认与回测区间一致；
    做多区间验证时把信号区间设成覆盖所有子区间的宽区间，**信号只算一次**，
    之后每个子区间直接切片（这也是唯一能省下重复算力的做法）。
    """
    cfg = config or BacktestConfig()
    sig_start = signal_start or start
    sig_end = signal_end or end
    # 股票池按"信号区间"取，而不是回测区间 —— 否则每个子区间的池子都不同，
    # 缓存键随之变化，多区间验证就没法复用同一份信号了。
    codes = data.load_symbols(db_path=db_path, start=sig_start, end=sig_end, min_rows=min_history)
    code_list = list(codes)
    if top_n:
        code_list = code_list[:top_n]
    if verbose:
        print(f"股票池 {len(code_list)} 只，区间 {start} ~ {end}，策略 {len(strategies)} 个")
        if (sig_start, sig_end) != (start, end):
            print(f"信号计算区间 {sig_start} ~ {sig_end}（回测时切片到 {start} ~ {end}）")

    signals_full = generate_signals(
        strategies,
        code_list,
        sig_start,
        sig_end,
        db_path=db_path,
        khunter_root=khunter_root,
        jobs=jobs,
        window=window,
        min_history=min_history,
        use_cache=use_cache,
        cache_dir=cache_dir,
        verbose=verbose,
    )
    lo = pd.to_datetime(start)
    hi = pd.to_datetime(end)
    signals: dict[str, pd.DataFrame] = {}
    for name, frame in signals_full.items():
        # 无信号的策略会返回空 DataFrame（默认 RangeIndex），不能直接做日期比较
        if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
            signals[name] = frame
            continue
        signals[name] = frame.loc[(frame.index >= lo) & (frame.index <= hi)]
    if regime_filter is not None:
        # 只在允许的交易日开新仓；已持仓不受影响（等价于"当日不发新信号"）
        for name, frame in signals.items():
            if frame.empty:
                continue
            allow = regime_filter.reindex(frame.index).fillna(False).astype(bool)
            signals[name] = frame.where(allow, other=np.nan)
        if verbose:
            print(f"  已应用状态过滤：{int(regime_filter.sum())}/{len(regime_filter)} 天允许开仓")
    close, high, low = price_frames(code_list, start, end, db_path=db_path)

    benchmark_series = None
    if benchmark:
        try:
            benchmark_series = load_benchmark(benchmark, start, end, cache_dir=cache_dir)
            if verbose:
                print(
                    f"  基准 {benchmark}: {len(benchmark_series)} 个交易日 "
                    f"({benchmark_series.index[0].date()} ~ {benchmark_series.index[-1].date()})"
                )
        except Exception as exc:
            if verbose:
                print(f"  ! 基准 {benchmark} 加载失败，跳过超额收益计算: {exc}")

    results: dict[str, BacktestResult] = {}
    for name, sig in signals.items():
        if sig.empty:
            if verbose:
                print(f"  ! {name} 无信号，跳过回测")
            continue
        results[name] = run_backtest(
            sig,
            close,
            high=high,
            low=low,
            benchmark=benchmark_series,
            benchmark_name=str(benchmark or ""),
            strategy=name,
            config=cfg,
        )

    if results:
        table = pd.DataFrame([r.as_row() for r in results.values()])
        table = table.sort_values("年化收益", ascending=False).reset_index(drop=True)
    else:
        table = pd.DataFrame()

    # 可选：写入 KHunter 的 backtest_result / backtest_trade，Web 端「回测历史」可见
    if save and results:
        try:
            from vector_bt.persistence import save_backtest_result

            for name, res in results.items():
                rid = save_backtest_result(
                    res,
                    backtest_name=backtest_name or f"向量化回测 {start}~{end}",
                    initial_capital=cfg.initial_capital,
                    max_daily_buys=cfg.max_daily_buys,
                    db_path=db_path,
                )
                if verbose:
                    print(f"  ✓ 已写入回测记录 #{rid}: {name}", flush=True)
        except Exception as exc:
            if verbose:
                print(f"  ! 写入回测记录失败: {exc}", flush=True)
    return table, results, signals


def sweep(
    strategies: Sequence[str],
    *,
    start: str,
    end: str,
    stop_losses: Sequence[float] = (-0.05, -0.07, -0.10, -0.15),
    take_profits: Sequence[float] = (0.10, 0.15, 0.21, 0.30),
    hold_periods: Sequence[int] = (5, 10, 20),
    db_path: str | Path | None = None,
    khunter_root: str | Path | None = None,
    jobs: int = 1,
    window: int = 180,
    min_history: int = 60,
    config: BacktestConfig | None = None,
    benchmark: str | None = DEFAULT_BENCHMARK,
    regime_filter: pd.Series | None = None,
    use_cache: bool = True,
    cache_dir: Path | str | None = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """参数扫描：固定信号，只变止损/止盈/持有期。

    因为信号已缓存，每个组合只是一次矩阵运算（秒级），
    所以这一步的瓶颈是组合数而不是数据量。
    """
    base = config or BacktestConfig()
    codes = list(
        data.load_symbols(db_path=db_path, start=start, end=end, min_rows=min_history)
    )
    signals = generate_signals(
        strategies,
        codes,
        start,
        end,
        db_path=db_path,
        khunter_root=khunter_root,
        jobs=jobs,
        window=window,
        min_history=min_history,
        use_cache=use_cache,
        cache_dir=cache_dir,
        verbose=verbose,
    )
    close, high, low = price_frames(codes, start, end, db_path=db_path)
    if regime_filter is not None:
        for name, frame in signals.items():
            if frame.empty:
                continue
            allow = regime_filter.reindex(frame.index).fillna(False).astype(bool)
            signals[name] = frame.where(allow, other=np.nan)
    benchmark_series = None
    if benchmark:
        try:
            benchmark_series = load_benchmark(benchmark, start, end, cache_dir=cache_dir)
        except Exception as exc:
            if verbose:
                print(f"  ! 基准加载失败: {exc}")

    rows: list[dict] = []
    total = len(strategies) * len(stop_losses) * len(take_profits) * len(hold_periods)
    done = 0
    for name, sig in signals.items():
        if sig.empty:
            continue
        for stop in stop_losses:
            for take in take_profits:
                for hold in hold_periods:
                    cfg = replace(
                        base, stop_loss=float(stop), take_profit=float(take), hold_period=int(hold)
                    )
                    result = run_backtest(
                        sig,
                        close,
                        high=high,
                        low=low,
                        benchmark=benchmark_series,
                        benchmark_name=str(benchmark or ""),
                        strategy=name,
                        config=cfg,
                    )
                    row = result.as_row()
                    row.update({"止损": float(stop), "止盈": float(take), "持有期": int(hold)})
                    rows.append(row)
                    done += 1
    if verbose:
        print(f"  完成 {done}/{total} 个参数组合")
    table = pd.DataFrame(rows)
    if not table.empty:
        table = table.sort_values("超额年化", ascending=False).reset_index(drop=True)
    return table
