# -*- coding: utf-8 -*-
"""ADR-151：百萬根六組 MA 效能與實際 SQLite 31 欄產品 shadow 驗收。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import engine_router, indicators, jae, migration_baseline, native_bridge
from data import kbars_store


MA_PERIODS = [5, 10, 20, 60, 120, 240]
MA_TYPES = ['SMA', 'EMA', 'WMA', 'SMA', 'EMA', 'WMA']
ROUTE_SETTINGS = {
    'ma_flags': [True] * 6, 'ma_types': MA_TYPES, 'ma_periods': MA_PERIODS,
    'bb_enabled': True, 'bb_period': 20,
    'bb_std_up': 2.1, 'bb_std_down': 1.8, 'bb_ma_type': 'EMA',
    'bb2_enabled': True, 'bb2_period': 55,
    'bb2_std_up': 2.5, 'bb2_std_down': 2.2, 'bb2_ma_type': 'WMA',
    'macd_enabled': True, 'macd_fast': 10, 'macd_slow': 24,
    'macd_signal': 7, 'rsi_enabled': True, 'rsi_period': 13,
    'kdj_enabled': True, 'kdj_period': 11, 'k_smooth': 4, 'd_smooth': 5,
    'dmi_enabled': True, 'dmi_period': 17,
    'jae_enabled': True,
    'jae_params': {'a_period': 13, 'j_n': 10, 'j_m1': 2,
                   'j_m2': 4, 'e_period': 55},
}


def _case_id(symbol, asset_type, timeframe):
    raw = f'{symbol}|{asset_type}|{timeframe}'.encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:12]


def _python_product_result(frame):
    result = indicators.calculate_indicators(
        frame, ROUTE_SETTINGS['ma_flags'], MA_TYPES, MA_PERIODS,
        bb_show=True, bbw_show=True,
        bb_period=ROUTE_SETTINGS['bb_period'],
        bb_std_up=ROUTE_SETTINGS['bb_std_up'],
        bb_std_dn=ROUTE_SETTINGS['bb_std_down'],
        bb_type=ROUTE_SETTINGS['bb_ma_type'],
        bb2_show=True, bb2_period=ROUTE_SETTINGS['bb2_period'],
        bb2_std_up=ROUTE_SETTINGS['bb2_std_up'],
        bb2_std_dn=ROUTE_SETTINGS['bb2_std_down'],
        bb2_type=ROUTE_SETTINGS['bb2_ma_type'],
        macd_show=True, macd_f=ROUTE_SETTINGS['macd_fast'],
        macd_s=ROUTE_SETTINGS['macd_slow'], macd_sig=ROUTE_SETTINGS['macd_signal'],
        rsi_show=True, rsi_p=ROUTE_SETTINGS['rsi_period'],
        kdj_show=True, kd_n=ROUTE_SETTINGS['kdj_period'],
        kd_m1=ROUTE_SETTINGS['k_smooth'], kd_m2=ROUTE_SETTINGS['d_smooth'],
        dmi_show=True, dmi_n=ROUTE_SETTINGS['dmi_period'])
    jae.compute(result, ROUTE_SETTINGS['jae_params'])
    return result


def _reference_ma(close, period, kind):
    series = pd.Series(close)
    if kind == 'SMA':
        return series.rolling(period).mean().to_numpy(np.float64)
    if kind == 'EMA':
        return series.ewm(span=period, adjust=False).mean().to_numpy(np.float64)
    weights = np.arange(1, period + 1, dtype=np.float64)
    result = np.full(close.size, np.nan, dtype=np.float64)
    result[period - 1:] = np.convolve(close, weights[::-1], mode='valid') / weights.sum()
    return result


def _synthetic_benchmark(module, rows):
    x = np.arange(rows, dtype=np.float64)
    close = 100.0 + x * 0.00003 + np.sin(x / 19.0) * 2.5 + np.cos(x / 71.0)
    frame = pd.DataFrame({
        'Open': close, 'High': close + 1.0, 'Low': close - 1.0,
        'Close': close, 'Volume': np.ones(rows, dtype=np.float64),
    }, index=pd.date_range('2020-01-01', periods=rows, freq='s'))
    batch = native_bridge.prepare_kbars(frame)

    started = perf_counter()
    native = native_bridge.calculate_native_moving_averages(
        module, batch, MA_PERIODS, MA_TYPES)
    native_ms = (perf_counter() - started) * 1000.0
    started = perf_counter()
    reference = [_reference_ma(close, period, kind)
                 for period, kind in zip(MA_PERIODS, MA_TYPES)]
    reference_ms = (perf_counter() - started) * 1000.0
    errors = []
    for actual, expected in zip(native.values, reference):
        finite = np.isfinite(expected)
        errors.append(float(np.max(np.abs(actual[finite] - expected[finite]), initial=0.0)))
        if not np.array_equal(np.isnan(actual), np.isnan(expected)):
            raise RuntimeError('synthetic MA NaN mask 不一致')
        if not np.allclose(actual[finite], expected[finite], rtol=2e-11, atol=2e-11):
            raise RuntimeError('synthetic MA 數值不一致')
    return {
        'rows': rows, 'columns': len(MA_PERIODS), 'periods': MA_PERIODS,
        'types': MA_TYPES, 'native_ms': native_ms, 'reference_ms': reference_ms,
        'reference': 'pandas rolling/ewm + NumPy convolution',
        'speedup_vs_reference': reference_ms / native_ms,
        'max_abs_error': max(errors),
    }


def build_report(database, cases, rows=1_000_000):
    probe = engine_router.probe_native()
    module, _info = native_bridge.load_native()
    module_path = Path(probe['module_file'])
    outputs = []
    for symbol, asset_type, timeframe in cases:
        frame = kbars_store.load(database, symbol, asset_type, timeframe)
        if frame is None or frame.empty:
            raise RuntimeError(f'SQLite case 無資料：{symbol}/{asset_type}/{timeframe}')
        routed = engine_router.route_indicators(
            frame, _python_product_result(frame), ROUTE_SETTINGS, mode='shadow')
        telemetry = routed.telemetry
        outputs.append({
            'case_id': _case_id(symbol, asset_type, timeframe),
            'asset_type': asset_type, 'timeframe': timeframe, 'rows': len(frame),
            'first_timestamp': frame.index[0].isoformat(),
            'last_timestamp': frame.index[-1].isoformat(),
            'status': telemetry['status'],
            'compared_columns': telemetry['compared_columns'],
            'max_abs_error': telemetry['max_abs_error'],
            'native_ms': telemetry['native_ms'],
        })
    return migration_baseline.canonicalize({
        'report_schema_version': 1,
        'environment': migration_baseline.environment_record(),
        'runtime': {
            'native_version': probe['native_version'],
            'python_cache_tag': sys.implementation.cache_tag,
            'module_sha256': hashlib.sha256(module_path.read_bytes()).hexdigest(),
        },
        'multi_ma': _synthetic_benchmark(module, rows),
        'route_mode': 'shadow', 'supported_columns': 31, 'cases': outputs,
    })


def _parse_case(value):
    parts = value.split('|')
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError('case 格式必須是 SYMBOL|ASSET_TYPE|TIMEFRAME')
    return tuple(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description='ADR-151 multi MA benchmark')
    parser.add_argument('--database', required=True)
    parser.add_argument('--case', action='append', type=_parse_case, required=True)
    parser.add_argument('--rows', type=int, default=1_000_000)
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    report = build_report(args.database, args.case, rows=args.rows)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        Path(args.output).write_text(rendered, encoding='utf-8')
    else:
        print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
