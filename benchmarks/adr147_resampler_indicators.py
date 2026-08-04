# -*- coding: utf-8 -*-
"""ADR-147：百萬根 C++ resampler／第一批指標核心 A/B benchmark。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import migration_baseline, native_bridge
from native import build_native


def _load_module(path):
    spec = importlib.util.spec_from_file_location('_stockbuild_native', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'無法載入 native module: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _measure(function, repeats=3, warmups=1):
    for _ in range(warmups):
        function()
    samples = []
    last = None
    for _ in range(repeats):
        started = time.perf_counter()
        last = function()
        samples.append((time.perf_counter() - started) * 1000.0)
    return last, {
        'samples_ms': samples,
        'median_ms': statistics.median(samples),
        'min_ms': min(samples),
        'max_ms': max(samples),
    }


def _python_resample(frame):
    return frame.resample('5min', label='left', closed='left').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min',
        'Close': 'last', 'Volume': 'sum',
    }).dropna()


def _python_indicators(close):
    sma = close.rolling(20).mean()
    ema = close.ewm(span=20, adjust=False).mean()
    weights = np.arange(1, 21, dtype=np.float64)
    wma = close.rolling(20).apply(
        lambda values: np.dot(values, weights) / weights.sum(), raw=True)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss)
    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    macd = fast - slow
    signal = macd.ewm(span=9, adjust=False).mean()
    std = close.rolling(20).std()
    upper = sma + 2.0 * std
    lower = sma - 2.0 * std
    return {
        'sma': sma.to_numpy(np.float64),
        'ema': ema.to_numpy(np.float64),
        'wma': wma.to_numpy(np.float64),
        'rsi': rsi.to_numpy(np.float64),
        'macd': macd.to_numpy(np.float64),
        'signal': signal.to_numpy(np.float64),
        'hist': (macd - signal).to_numpy(np.float64),
        'bb_mid': sma.to_numpy(np.float64),
        'bb_std': std.to_numpy(np.float64),
        'bb_upper': upper.to_numpy(np.float64),
        'bb_lower': lower.to_numpy(np.float64),
        'bb_width': (((upper - lower) / sma) * 100).to_numpy(np.float64),
    }


def build_report(rows=1_000_000, repeats=3, warmups=1):
    if rows < 100 or rows > 1_000_000:
        raise ValueError('rows 必須介於 100 與 1,000,000')
    built = build_native.build(ROOT / 'native' / 'build-benchmark', config='Release')
    module_path = Path(built['module'])
    module = _load_module(module_path)
    native_bridge.validate_abi_info(dict(module.abi_info()))

    index = pd.date_range('2024-01-01', periods=rows, freq='min')
    x = np.arange(rows, dtype=np.float64)
    close = 100.0 + x * 0.0001 + np.sin(x / 17.0) * 2.5 + np.cos(x / 101.0)
    frame = pd.DataFrame({
        'Open': close - 0.1,
        'High': close + 0.8,
        'Low': close - 0.7,
        'Close': close,
        'Volume': 1000.0 + (x % 100),
    }, index=index)
    batch = native_bridge.prepare_kbars(frame)

    native_resampled, native_resample_timing = _measure(
        lambda: native_bridge.resample_kbars(
            module, batch, 'fixed', interval_minutes=5), repeats, warmups)
    python_resampled, python_resample_timing = _measure(
        lambda: _python_resample(frame), repeats, warmups)
    np.testing.assert_array_equal(
        native_resampled.timestamps, python_resampled.index.as_unit('ns').asi8)
    for actual, name in zip(
            native_resampled.as_args()[1:6], ('Open', 'High', 'Low', 'Close', 'Volume')):
        np.testing.assert_allclose(actual, python_resampled[name].to_numpy(np.float64))

    native_indicators, native_indicator_timing = _measure(
        lambda: native_bridge.calculate_native_indicators(module, batch), repeats, warmups)
    python_indicators, python_indicator_timing = _measure(
        lambda: _python_indicators(frame['Close']), repeats, warmups)
    max_abs_error_x1e12_by_indicator = {}
    bb_columns = {'bb_std', 'bb_upper', 'bb_lower', 'bb_width'}
    for name, actual in native_indicators.as_dict().items():
        expected = python_indicators[name]
        np.testing.assert_array_equal(
            np.isnan(actual), np.isnan(expected), err_msg=f'{name} NaN mask')
        finite = np.isfinite(actual) & np.isfinite(expected)
        max_abs_error_x1e12_by_indicator[name] = (
            float(np.max(np.abs(actual[finite] - expected[finite]))) * 1e12
            if np.any(finite) else 0.0
        )
        rtol, atol = ((1e-8, 1e-9) if name in bb_columns else (2e-10, 2e-10))
        np.testing.assert_allclose(
            actual, expected, rtol=rtol, atol=atol,
            equal_nan=True, err_msg=name)

    native_resample_median = native_resample_timing['median_ms']
    python_resample_median = python_resample_timing['median_ms']
    native_indicator_median = native_indicator_timing['median_ms']
    python_indicator_median = python_indicator_timing['median_ms']
    return migration_baseline.canonicalize({
        'report_schema_version': 1,
        'environment': migration_baseline.environment_record(),
        'compiler': built['compiler'],
        'native_module_sha256': hashlib.sha256(module_path.read_bytes()).hexdigest(),
        'rows': rows,
        'repeats': repeats,
        'warmups': warmups,
        'workload': {
            'resample': '1-minute to 5-minute OHLCV',
            'indicators': 'SMA/EMA/WMA(20), RSI(14), MACD(12,26,9), BB(20,2,2)',
        },
        'timings': {
            'native_resample': native_resample_timing,
            'python_pandas_resample': python_resample_timing,
            'native_indicators': native_indicator_timing,
            'python_pandas_indicators': python_indicator_timing,
        },
        'result': {
            'resampled_rows': native_resampled.rows,
            'indicator_rows': native_indicators.rows,
            'resample_speedup_vs_python':
                python_resample_median / native_resample_median,
            'indicator_speedup_vs_python':
                python_indicator_median / native_indicator_median,
            'native_resample_rows_per_second':
                rows / (native_resample_median / 1000.0),
            'native_indicator_rows_per_second':
                rows / (native_indicator_median / 1000.0),
            'all_resample_columns_readonly': all(
                not column.flags.writeable for column in native_resampled.as_args()),
            'all_indicator_columns_readonly': all(
                not column.flags.writeable
                for column in native_indicators.as_dict().values()),
            'max_abs_error_x1e12_by_indicator': max_abs_error_x1e12_by_indicator,
            'last_hist': native_indicators.hist[-1],
        },
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description='ADR-147 resampler/indicator benchmark')
    parser.add_argument('--rows', type=int, default=1_000_000)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--warmups', type=int, default=1)
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    payload = build_report(args.rows, args.repeats, args.warmups)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        Path(args.output).write_text(rendered, encoding='utf-8')
    print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
