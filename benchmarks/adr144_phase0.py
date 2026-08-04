# -*- coding: utf-8 -*-
"""ADR-144 Phase 0：Python 參考引擎 golden fixture 與效能基準。

常用命令：
  python benchmarks/adr144_phase0.py --profile smoke
  python benchmarks/adr144_phase0.py --profile smoke --golden
  python benchmarks/adr144_phase0.py --profile full --output <report.json>

全程離線，不 import tkinter/shioaji，也不會連線或下單。
"""
from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from core import backtest, fundamental_parser as fp, market_screener, optimizer
from core import migration_baseline
from data import kbars_store, market_store


GOLDEN_SOURCE = "adr144-synthetic-v1"
STRATEGY_SOURCE = """def on_bar(ctx):
    fast = ctx.ema(ctx.param('fast', 7))
    slow = ctx.sma(ctx.param('slow', 25))
    if ctx.cross_up(fast, slow) and ctx.position in ('FLAT', 'SHORT'):
        return ctx.buy()
    if ctx.cross_down(fast, slow) and ctx.position in ('FLAT', 'LONG'):
        return ctx.sell()
    return ctx.hold()
"""

PROFILES = {
    "smoke": {
        "backtest_bars": 400,
        "optimizer_bars": 180,
        "optimizer_fast": [5, 7],
        "optimizer_slow": [20, 25],
        "screener_symbols": 12,
        "screener_bars": 90,
        "repeats": 2,
        "warmups": 1,
    },
    "reference": {
        "backtest_bars": 5_000,
        "optimizer_bars": 400,
        "optimizer_fast": list(range(3, 23, 2)),
        "optimizer_slow": list(range(20, 70, 5)),
        "screener_symbols": 300,
        "screener_bars": 520,
        "repeats": 3,
        "warmups": 1,
    },
    "full": {
        "backtest_bars": 100_000,
        "optimizer_bars": 600,
        "optimizer_fast": list(range(3, 53, 5)),
        "optimizer_slow": list(range(20, 270, 5)),
        "screener_symbols": 2_000,
        "screener_bars": 2_600,
        "repeats": 3,
        "warmups": 1,
    },
}


def synthetic_ohlcv(n, seed=144):
    rng = np.random.RandomState(seed)
    index = pd.date_range("2022-01-03 09:00:00", periods=int(n), freq="min")
    wave = np.sin(np.arange(n, dtype=float) / 17.0) * 7.0
    close = 15_000.0 + np.cumsum(rng.normal(0.0, 12.0, n)) + wave
    open_ = close + rng.normal(0.0, 2.0, n)
    spread = np.abs(rng.normal(4.0, 1.0, n))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.randint(100, 20_000, n).astype(float)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low,
                         "Close": close, "Volume": volume}, index=index)


def strategy_fixture():
    return {
        "name": "ADR-144 deterministic SAR",
        "kind": "custom",
        "trade_type": "期貨",
        "market": "台期貨",
        "symbol": "TXF",
        "qty": 1,
        "direction": "做多",
        "timeframe": "1分K",
        "source_code": STRATEGY_SOURCE,
        "entry": [],
        "exit_signals": [],
        "custom_params": {},
    }


def screener_fixture(symbols, bars, seed=244):
    rng = np.random.RandomState(seed)
    dates = pd.date_range("2020-01-02", periods=int(bars), freq="B")
    fundamentals = []
    daily_parts = []
    for i in range(int(symbols)):
        code = f"{1000 + i:04d}"
        rising = (i % 3) != 0
        base = 20.0 + (i % 80)
        drift = np.linspace(0.0, 18.0 if rising else -10.0, len(dates))
        close = base + drift + rng.normal(0.0, 0.15, len(dates))
        volume = rng.randint(100, 50_000, len(dates)).astype(float)
        daily_parts.append(pd.DataFrame({
            "Date": dates.strftime("%Y-%m-%d"), "Code": code,
            "Name": f"股票{i:04d}", "Open": close, "High": close + 0.5,
            "Low": close - 0.5, "Close": close, "Volume": volume,
        }))
        fundamentals.append({
            fp.COL_CODE: code, fp.COL_NAME: f"股票{i:04d}",
            fp.COL_INDUSTRY: "半導體業" if i % 2 == 0 else "其他",
            fp.COL_CLOSE: float(close[-1]), fp.COL_PE: 8.0 + (i % 20),
            fp.COL_PB: 0.8 + (i % 6) * 0.2, fp.COL_YIELD: 2.0 + (i % 7),
            fp.COL_EPS: 1.0 + (i % 9), fp.COL_GROSS_MARGIN: 20.0 + (i % 30),
            fp.COL_REV_YOY: -5.0 + (i % 40), fp.COL_REV_MOM: float(i % 10),
            fp.COL_REVENUE: float(1_000_000 + i), fp.COL_EQUITY: float(5_000_000 + i),
            fp.COL_ROE: 3.0 + (i % 15),
        })
    return pd.DataFrame(fundamentals), pd.concat(daily_parts, ignore_index=True)


def run_backtest_fixture(n):
    return backtest.run_backtest(strategy_fixture(), synthetic_ohlcv(n, seed=144))


def run_optimizer_fixture(n, fast_values, slow_values):
    return optimizer.optimize(
        strategy_fixture(), synthetic_ohlcv(n, seed=145),
        {"fast": list(fast_values), "slow": list(slow_values)},
        objective="淨損益", min_trades=1, max_combos=500,
    )


def run_screener_fixture(symbols, bars):
    fundamental, daily = screener_fixture(symbols, bars)
    return market_screener.screen(
        fundamental, daily,
        conditions=[{"type": "price_above_ma", "params": {"n": 20, "kind": "SMA"}}],
        fundamental_conds=[
            {"field": "pe", "op": market_screener.OP_LTE, "value": 18},
            {"field": "yield", "op": market_screener.OP_GTE, "value": 4},
        ],
        to_ohlcv=market_store.to_ohlcv_frame,
        min_bars=30, max_results=int(symbols),
    )


def _sqlite_query_plan(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    # sqlite3.Connection.__exit__ 只 commit/rollback，不會 close；Windows 會因此
    # 鎖住 DB，TemporaryDirectory 退出時直接 WinError 32（ADR-142 / P-117）。
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "EXPLAIN QUERY PLAN SELECT ts, open, high, low, close, volume FROM kbars "
            "WHERE symbol=? AND asset_type=? AND timeframe=? AND ts>=? AND ts<=? ORDER BY ts",
            ("2330", "stock", "1分K", "2026-01-01", "2026-01-02"),
        ).fetchall()
    details = [str(row[-1]) for row in rows]
    return details, any("INDEX" in item.upper() for item in details)


def _sqlite_scenario(initial_rows, requested_rows=40):
    source = synthetic_ohlcv(requested_rows, seed=344)
    calls = []
    with tempfile.TemporaryDirectory() as temp_dir:
        path = str(Path(temp_dir) / "kbars.sqlite3")
        kbars_store.initialize(path)
        if initial_rows:
            kbars_store.upsert(path, "2330", "stock", "1分K", source.iloc[:initial_rows])
        first, last, count = kbars_store.coverage(path, "2330", "stock", "1分K")
        request_start, request_end = source.index[0], source.index[-1]
        missing = []
        if not count:
            missing.append((request_start, request_end))
        else:
            db_first, db_last = pd.Timestamp(first), pd.Timestamp(last)
            step = pd.Timedelta(minutes=1)
            if request_start < db_first:
                missing.append((request_start, min(request_end, db_first - step)))
            if request_end > db_last:
                missing.append((max(request_start, db_last + step), request_end))
        for start, end in missing:
            calls.append({"start": start.isoformat(), "end": end.isoformat()})
            part = source[(source.index >= start) & (source.index <= end)]
            kbars_store.upsert(path, "2330", "stock", "1分K", part)
        loaded = kbars_store.load(path, "2330", "stock", "1分K",
                                  start=request_start, end=request_end)
        final_first, final_last, final_count = kbars_store.coverage(
            path, "2330", "stock", "1分K")
        plan, uses_index = _sqlite_query_plan(path)
        component = {
            "initial_rows": initial_rows,
            "api_calls": len(calls),
            "download_ranges": calls,
            "loaded_rows": 0 if loaded is None else len(loaded),
            "loaded_hash": migration_baseline.stable_hash(
                [] if loaded is None else loaded.reset_index().to_dict("records")),
            "coverage": {"first": final_first, "last": final_last, "count": final_count},
            "uses_composite_index": uses_index,
        }
    return component, plan


def run_sqlite_fixtures():
    complete, plan = _sqlite_scenario(40)
    tail, _ = _sqlite_scenario(28)
    new_symbol, _ = _sqlite_scenario(0)
    return {"complete_hit": complete, "tail_increment": tail,
            "new_symbol": new_symbol}, plan


def _fixture_record(profile, cfg):
    return {
        "source": GOLDEN_SOURCE,
        "profile": profile,
        "seeds": {"backtest": 144, "optimizer": 145, "screener": 244, "sqlite": 344},
        "sizes": {key: value for key, value in cfg.items()
                  if key not in ("repeats", "warmups")},
    }


def build_golden(profile="smoke"):
    cfg = copy.deepcopy(PROFILES[profile])
    backtest_result = run_backtest_fixture(cfg["backtest_bars"])
    optimizer_result = run_optimizer_fixture(
        cfg["optimizer_bars"], cfg["optimizer_fast"], cfg["optimizer_slow"])
    screener_result = run_screener_fixture(cfg["screener_symbols"], cfg["screener_bars"])
    sqlite_result, _plan = run_sqlite_fixtures()
    return migration_baseline.make_golden_bundle({
        "backtest": backtest_result,
        "optimizer": optimizer_result,
        "screener": screener_result,
        "sqlite": sqlite_result,
    }, _fixture_record(profile, cfg))


def _measure(function, repeats, warmups):
    for _ in range(int(warmups)):
        function()
    samples = []
    last = None
    for _ in range(int(repeats)):
        start = time.perf_counter()
        last = function()
        samples.append((time.perf_counter() - start) * 1000.0)
    return last, {
        "samples_ms": samples,
        "median_ms": statistics.median(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
    }


def build_report(profile="smoke"):
    cfg = copy.deepcopy(PROFILES[profile])
    repeats, warmups = cfg["repeats"], cfg["warmups"]
    bt, bt_time = _measure(lambda: run_backtest_fixture(cfg["backtest_bars"]), repeats, warmups)
    opt, opt_time = _measure(
        lambda: run_optimizer_fixture(cfg["optimizer_bars"], cfg["optimizer_fast"],
                                      cfg["optimizer_slow"]), repeats, warmups)
    screen, screen_time = _measure(
        lambda: run_screener_fixture(cfg["screener_symbols"], cfg["screener_bars"]),
        repeats, warmups)
    sql, sql_time = _measure(run_sqlite_fixtures, repeats, warmups)
    sql_components, query_plan = sql
    summaries = {
        "backtest": {"trades": len(bt["trades"]), "equity_rows": len(bt["equity"]),
                     "metrics_hash": migration_baseline.stable_hash(bt["metrics"])},
        "optimizer": {"evaluated": opt["evaluated"],
                      "best_hash": migration_baseline.stable_hash(opt["best"])},
        "screener": {"scanned": screen["scanned"], "passed": screen["passed"],
                     "rows_hash": migration_baseline.stable_hash(screen["rows"])},
        "sqlite": sql_components,
    }
    measured_bundle = migration_baseline.make_golden_bundle(
        {"backtest": bt, "optimizer": opt, "screener": screen, "sqlite": sql_components},
        _fixture_record(profile, cfg))
    return migration_baseline.canonicalize({
        "report_schema_version": 1,
        "profile": profile,
        "source": GOLDEN_SOURCE,
        "environment": migration_baseline.environment_record(),
        "workload": cfg,
        "timings": {"backtest": bt_time, "optimizer": opt_time,
                    "screener": screen_time, "sqlite_scenarios": sql_time},
        "summaries": summaries,
        "sqlite_query_plan": query_plan,
        "golden_bundle_hash": measured_bundle["bundle_hash"],
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description="ADR-144 Phase 0 離線基準")
    parser.add_argument("--profile", choices=tuple(PROFILES), default="smoke")
    parser.add_argument("--golden", action="store_true", help="輸出完整黃金 bundle")
    parser.add_argument("--output", help="另存 UTF-8 JSON 報告；預設只輸出 stdout")
    args = parser.parse_args(argv)
    payload = build_golden(args.profile) if args.golden else build_report(args.profile)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
