# -*- coding: utf-8 -*-
"""ADR-150：從正式 runtime 載入 C++，執行產品指標 router 的實際 SQLite shadow。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import engine_router, indicators, jae, migration_baseline
from data import kbars_store


ROUTE_SETTINGS = {
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
        frame, [False] * 6, ['SMA'] * 6, [5] * 6,
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


def build_report(database, cases):
    probe = engine_router.probe_native()
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
            'asset_type': asset_type,
            'timeframe': timeframe,
            'rows': len(frame),
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
        'route_mode': 'shadow',
        'supported_columns': 25,
        'cases': outputs,
    })


def _parse_case(value):
    parts = value.split('|')
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError('case 格式必須是 SYMBOL|ASSET_TYPE|TIMEFRAME')
    return tuple(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description='ADR-150 product shadow runner')
    parser.add_argument('--database', required=True)
    parser.add_argument('--case', action='append', type=_parse_case, required=True)
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    report = build_report(args.database, args.case)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        Path(args.output).write_text(rendered, encoding='utf-8')
    else:
        print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
