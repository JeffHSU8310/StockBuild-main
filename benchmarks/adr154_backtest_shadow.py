# -*- coding: utf-8 -*-
"""ADR-154 authoritative Python backtest plus native T+1 intent shadow benchmark."""
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

from core import migration_baseline, native_backtest_shadow, native_bridge


def synthetic(rows):
    x = np.arange(rows, dtype=np.float64)
    close = 100.0 + np.sin(x / 11.0) * 7.0 + np.cos(x / 31.0) * 2.0
    open_ = close + np.sin(x / 5.0) * 0.8
    return pd.DataFrame({
        'Open': open_, 'High': np.maximum(open_, close) + 1.0,
        'Low': np.minimum(open_, close) - 1.0, 'Close': close,
        'Volume': 1000.0 + (x % 17) * 90.0,
    }, index=pd.date_range('2020-01-01', periods=rows, freq='min'))


STRATEGY = {
    'symbol': '2330', 'trade_type': '零股', 'direction': '做多', 'qty': 2,
    'entry': [
        {'type': 'price_cross_up_ma', 'params': {'n': 20, 'kind': 'EMA'}},
        {'type': 'volume_above_ma', 'params': {'n': 20, 'mult': 1.1}},
    ],
    'exit_signals': [
        {'type': 'price_cross_down_ma', 'params': {'n': 20, 'kind': 'EMA'}},
        {'type': 'pct_change_below', 'params': {'value': -2.0}},
    ],
    'stop_loss_pct': 2.0, 'take_profit_pct': 4.0,
    'stop_loss_abs': 0.0, 'take_profit_abs': 0.0,
    'intrabar_stop': False,
}


def build_report(rows=100_000):
    module, info = native_bridge.load_native()
    frame = synthetic(rows)
    started = perf_counter()
    report = native_backtest_shadow.run_shadow(
        module, STRATEGY, frame, apply_cost_model=False)
    elapsed_ms = (perf_counter() - started) * 1000.0
    return migration_baseline.canonicalize({
        'report_schema_version': 1,
        'environment': migration_baseline.environment_record(),
        'runtime': {'native_version': info['native_version'],
                    'python_cache_tag': sys.implementation.cache_tag},
        'scope': {'rows': rows, 'broker_calls': 0, 'authoritative_engine': 'python'},
        'parity': {
            'status': report['status'],
            'python_intents': report['python_intents'],
            'native_intents': report['native_intents'],
            'mismatches': len(report['mismatches']),
        },
        'performance': {
            'full_shadow_ab_ms': elapsed_ms,
            'bars_per_second': rows / (elapsed_ms / 1000.0),
        },
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description='ADR-154 native backtest shadow benchmark')
    parser.add_argument('--rows', type=int, default=100_000)
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    rendered = json.dumps(build_report(args.rows), ensure_ascii=False, indent=2) + '\n'
    if args.output:
        Path(args.output).write_text(rendered, encoding='utf-8')
    else:
        print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
