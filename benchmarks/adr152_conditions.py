# -*- coding: utf-8 -*-
"""ADR-152：第一批 native 策略條件逐根 differential 與 10 萬根效能基準。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import migration_baseline, native_bridge, strategy_engine


CONDITIONS = [
    {'type': 'ma_cross_up', 'params': {'fast': 5, 'slow': 20}},
    {'type': 'ma_cross_down', 'params': {'fast': 7, 'slow': 25}},
    {'type': 'price_break_high', 'params': {'n': 12}},
    {'type': 'price_break_low', 'params': {'n': 15}},
    {'type': 'price_above', 'params': {'value': 105}},
    {'type': 'price_below', 'params': {'value': 98}},
    {'type': 'price_cross_up_ma', 'params': {'n': 13, 'kind': 'EMA'}},
    {'type': 'price_cross_down_ma', 'params': {'n': 18, 'kind': 'SMA'}},
    {'type': 'price_above_ma', 'params': {'n': 21, 'kind': 'EMA'}},
    {'type': 'price_below_ma', 'params': {'n': 21, 'kind': 'SMA'}},
    {'type': 'ma_slope_up', 'params': {'n': 16, 'look': 4, 'kind': 'EMA'}},
    {'type': 'ma_slope_down', 'params': {'n': 16, 'look': 5, 'kind': 'SMA'}},
    {'type': 'ma_align_bull', 'params': {'short': 5, 'mid': 13, 'long': 34, 'kind': 'EMA'}},
    {'type': 'ma_align_bear', 'params': {'short': 6, 'mid': 15, 'long': 40, 'kind': 'SMA'}},
    {'type': 'volume_above_ma', 'params': {'n': 10, 'mult': 1.1}},
    {'type': 'volume_below_ma', 'params': {'n': 12, 'mult': 0.9}},
    {'type': 'consecutive_up', 'params': {'n': 3}},
    {'type': 'consecutive_down', 'params': {'n': 2}},
    {'type': 'pct_change_above', 'params': {'value': 1.2}},
    {'type': 'pct_change_below', 'params': {'value': -1.0}},
    {'type': 'always_true', 'params': {}}, {'type': 'inside_bar', 'params': {}},
    {'type': 'gap_up', 'params': {'value': 0.1}},
    {'type': 'gap_down', 'params': {'value': 0.1}},
]


def synthetic(rows):
    x = np.arange(rows, dtype=np.float64)
    close = 100.0 + np.sin(x / 5.0) * 8.0 + np.cos(x / 17.0) * 3.0 + x * 0.0001
    open_ = close + np.sin(x / 3.0) * 1.5
    return pd.DataFrame({
        'Open': open_, 'High': np.maximum(open_, close) + 1.0 + (x % 4) * 0.2,
        'Low': np.minimum(open_, close) - 1.0 - (x % 3) * 0.15,
        'Close': close, 'Volume': 800.0 + (x % 11) * 150.0 + np.sin(x) * 50.0,
    }, index=pd.date_range('2025-01-01', periods=rows, freq='min'))


def native_run(module, frame):
    return native_bridge.calculate_native_conditions(
        module, native_bridge.prepare_kbars(frame), CONDITIONS)


def python_run(frame):
    columns = []
    for condition in CONDITIONS:
        function = strategy_engine.CONDITIONS[condition['type']][2]
        columns.append(np.array([
            bool(function(frame.iloc[:index + 1], condition['params']))
            for index in range(len(frame))
        ], dtype=np.uint8))
    return columns


def build_report(rows=100_000, parity_rows=600):
    module, info = native_bridge.load_native()
    parity_frame = synthetic(parity_rows)
    started = perf_counter(); native_small = native_run(module, parity_frame)
    native_small_ms = (perf_counter() - started) * 1000.0
    started = perf_counter(); python = python_run(parity_frame)
    python_ms = (perf_counter() - started) * 1000.0
    for condition, actual, expected in zip(CONDITIONS, native_small.values, python):
        if not np.array_equal(actual, expected):
            raise RuntimeError(f"condition differential 失敗：{condition['type']}")
    large_frame = synthetic(rows)
    native_run(module, large_frame)  # warm-up
    started = perf_counter(); native_large = native_run(module, large_frame)
    native_large_ms = (perf_counter() - started) * 1000.0
    return migration_baseline.canonicalize({
        'report_schema_version': 1,
        'environment': migration_baseline.environment_record(),
        'runtime': {'native_version': info['native_version'],
                    'python_cache_tag': sys.implementation.cache_tag},
        'conditions': len(CONDITIONS),
        'parity': {
            'rows': parity_rows, 'status': 'passed',
            'native_ms': native_small_ms, 'python_current_ms': python_ms,
            'speedup_vs_python_current': python_ms / native_small_ms,
            'signal_hash': migration_baseline.stable_hash(
                [value.tolist() for value in native_small.values]),
        },
        'performance': {
            'rows': rows, 'native_ms': native_large_ms,
            'bar_conditions_per_second': rows * len(CONDITIONS) / (native_large_ms / 1000.0),
            'signal_hash': migration_baseline.stable_hash(
                [value.tolist() for value in native_large.values]),
        },
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description='ADR-152 native conditions benchmark')
    parser.add_argument('--rows', type=int, default=100_000)
    parser.add_argument('--parity-rows', type=int, default=600)
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    rendered = json.dumps(build_report(args.rows, args.parity_rows),
                          ensure_ascii=False, indent=2) + '\n'
    if args.output:
        Path(args.output).write_text(rendered, encoding='utf-8')
    else:
        print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
