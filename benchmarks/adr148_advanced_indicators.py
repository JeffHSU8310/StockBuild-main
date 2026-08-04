# -*- coding: utf-8 -*-
"""ADR-148：KDJ／DMI／JAE 百萬筆 benchmark 與實際 SQLite shadow。"""
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
from data import kbars_store
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


def _kdj(frame, n=9, m1=3, m2=3):
    low_min = frame['Low'].rolling(n).min()
    high_max = frame['High'].rolling(n).max()
    rsv = 100 * (frame['Close'] - low_min) / (high_max - low_min)
    k = rsv.ewm(com=m1 - 1, adjust=False).mean()
    d = k.ewm(com=m2 - 1, adjust=False).mean()
    return rsv, k, d, 3 * k - 2 * d


def _python_advanced(frame):
    rsv, k, d, j = _kdj(frame)
    up_move = frame['High'].diff()
    down_move = -frame['Low'].diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=frame.index)
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=frame.index)
    tr = pd.concat([
        frame['High'] - frame['Low'],
        (frame['High'] - frame['Close'].shift()).abs(),
        (frame['Low'] - frame['Close'].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=14, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(span=14, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(span=14, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(span=14, adjust=False).mean()
    delta = frame['Close'].diff()
    gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    jae_a = 100 - 100 / (1 + gain / loss)
    jae_e = jae_a.ewm(span=60, adjust=False).mean()
    return {
        'rsv': rsv.to_numpy(np.float64),
        'k': k.to_numpy(np.float64),
        'd': d.to_numpy(np.float64),
        'j': j.to_numpy(np.float64),
        'plus_dm': plus_dm.to_numpy(np.float64),
        'minus_dm': minus_dm.to_numpy(np.float64),
        'plus_di': plus_di.to_numpy(np.float64),
        'minus_di': minus_di.to_numpy(np.float64),
        'adx': adx.to_numpy(np.float64),
        'jae_a': jae_a.to_numpy(np.float64),
        'jae_j': j.to_numpy(np.float64),
        'jae_e': jae_e.to_numpy(np.float64),
    }


def _compare_indicators(native_result, expected):
    errors = {}
    for name, actual in native_result.as_dict().items():
        reference = expected[name]
        np.testing.assert_array_equal(
            np.isnan(actual), np.isnan(reference), err_msg=f'{name} NaN mask')
        finite = np.isfinite(actual) & np.isfinite(reference)
        error = (float(np.max(np.abs(actual[finite] - reference[finite])))
                 if np.any(finite) else 0.0)
        errors[name] = error * 1e12
        np.testing.assert_allclose(
            actual, reference, rtol=2e-10, atol=2e-10,
            equal_nan=True, err_msg=name)
    return errors


def _synthetic_frame(rows):
    index = pd.date_range('2024-01-01', periods=rows, freq='min')
    x = np.arange(rows, dtype=np.float64)
    close = 100.0 + x * 0.0001 + np.sin(x / 17.0) * 2.5 + np.cos(x / 101.0)
    return pd.DataFrame({
        'Open': close - 0.1,
        'High': close + 0.8 + (x % 3) * 0.01,
        'Low': close - 0.7 - (x % 5) * 0.01,
        'Close': close,
        'Volume': 1000.0 + (x % 100),
    }, index=index)


def _case_id(symbol, asset_type, timeframe):
    raw = f'{symbol}|{asset_type}|{timeframe}'.encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:12]


def _real_shadow(module, database, cases):
    output = []
    for symbol, asset_type, timeframe in cases:
        frame = kbars_store.load(database, symbol, asset_type, timeframe)
        if frame is None or frame.empty:
            raise RuntimeError(f'SQLite case 無資料: {symbol}/{asset_type}/{timeframe}')
        native_range = native_bridge.read_sqlite_range(
            module, database, symbol, asset_type, timeframe)
        np.testing.assert_array_equal(
            native_range.batch.timestamps, frame.index.as_unit('ns').asi8)
        advanced = native_bridge.calculate_native_advanced_indicators(
            module, native_range.batch)
        errors = _compare_indicators(advanced, _python_advanced(frame))
        resampled = {}
        aggregation = {
            'Open': 'first', 'High': 'max', 'Low': 'min',
            'Close': 'last', 'Volume': 'sum',
        }
        for mode, rule in (('week', 'W-MON'), ('month', 'MS')):
            actual = native_bridge.resample_kbars(module, native_range.batch, mode)
            expected = frame.resample(rule, label='left', closed='left').agg(
                aggregation).dropna()
            np.testing.assert_array_equal(
                actual.timestamps, expected.index.as_unit('ns').asi8)
            for values, name in zip(
                    actual.as_args()[1:6], ('Open', 'High', 'Low', 'Close', 'Volume')):
                np.testing.assert_allclose(values, expected[name].to_numpy(np.float64))
            resampled[mode] = actual.rows
        output.append({
            'case_id': _case_id(symbol, asset_type, timeframe),
            'asset_type': asset_type,
            'timeframe': timeframe,
            'rows': len(frame),
            'first_timestamp': frame.index[0].isoformat(),
            'last_timestamp': frame.index[-1].isoformat(),
            'resampled_rows': resampled,
            'max_abs_error_x1e12_by_indicator': errors,
        })
    return output


def build_report(rows=1_000_000, repeats=3, warmups=1,
                 database=None, cases=()):
    if rows < 100 or rows > 1_000_000:
        raise ValueError('rows 必須介於 100 與 1,000,000')
    built = build_native.build(
        ROOT / 'native' / 'build-benchmark-advanced', config='Release')
    module_path = Path(built['module'])
    module = _load_module(module_path)
    native_bridge.validate_abi_info(dict(module.abi_info()))

    frame = _synthetic_frame(rows)
    batch = native_bridge.prepare_kbars(frame)
    native_result, native_timing = _measure(
        lambda: native_bridge.calculate_native_advanced_indicators(module, batch),
        repeats, warmups)
    python_result, python_timing = _measure(
        lambda: _python_advanced(frame), repeats, warmups)
    errors = _compare_indicators(native_result, python_result)
    native_median = native_timing['median_ms']
    python_median = python_timing['median_ms']
    real_cases = _real_shadow(module, database, cases) if database and cases else []
    return migration_baseline.canonicalize({
        'report_schema_version': 1,
        'environment': migration_baseline.environment_record(),
        'compiler': built['compiler'],
        'native_module_sha256': hashlib.sha256(module_path.read_bytes()).hexdigest(),
        'rows': rows,
        'repeats': repeats,
        'warmups': warmups,
        'workload': 'KDJ(9,3,3), DMI(14), JAE(14,9,3,3,60)',
        'timings': {
            'native_advanced_indicators': native_timing,
            'python_pandas_advanced_indicators': python_timing,
        },
        'result': {
            'speedup_vs_python': python_median / native_median,
            'native_rows_per_second': rows / (native_median / 1000.0),
            'all_columns_readonly': all(
                not column.flags.writeable
                for column in native_result.as_dict().values()),
            'max_abs_error_x1e12_by_indicator': errors,
        },
        'real_sqlite_shadow': real_cases,
    })


def _parse_case(value):
    parts = value.split('|')
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError('case 格式必須是 SYMBOL|ASSET_TYPE|TIMEFRAME')
    return tuple(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description='ADR-148 advanced indicator benchmark')
    parser.add_argument('--rows', type=int, default=1_000_000)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--warmups', type=int, default=1)
    parser.add_argument('--database')
    parser.add_argument('--case', action='append', type=_parse_case, default=[])
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    if bool(args.database) != bool(args.case):
        parser.error('--database 與至少一個 --case 必須一起提供')
    payload = build_report(
        args.rows, args.repeats, args.warmups, args.database, args.case)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        Path(args.output).write_text(rendered, encoding='utf-8')
    print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
