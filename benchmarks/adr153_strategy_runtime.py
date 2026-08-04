# -*- coding: utf-8 -*-
"""ADR-153 end-to-end condition plus stateful intent benchmark."""
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

from core import migration_baseline, native_bridge


ENTRY = [
    {'type': 'price_cross_up_ma', 'params': {'n': 20, 'kind': 'EMA'}},
    {'type': 'volume_above_ma', 'params': {'n': 20, 'mult': 1.1}},
]
EXIT = [
    {'type': 'price_cross_down_ma', 'params': {'n': 20, 'kind': 'EMA'}},
    {'type': 'pct_change_below', 'params': {'value': -2.0}},
]


def synthetic(rows):
    x = np.arange(rows, dtype=np.float64)
    close = 100.0 + np.sin(x / 11.0) * 7.0 + np.cos(x / 31.0) * 2.0 + x * 0.00001
    open_ = close + np.sin(x / 5.0) * 0.8
    return pd.DataFrame({
        'Open': open_, 'High': np.maximum(open_, close) + 1.0,
        'Low': np.minimum(open_, close) - 1.0, 'Close': close,
        'Volume': 1000.0 + (x % 17) * 90.0,
    }, index=pd.date_range('2020-01-01', periods=rows, freq='min'))


def run(module, frame):
    return native_bridge.evaluate_native_strategy(
        module, native_bridge.prepare_kbars(frame), ENTRY, EXIT,
        direction='LONG', quantity=2, entry_logic='AND', exit_logic='OR',
        stop_loss_pct=2.0, take_profit_pct=4.0, max_trades_per_day=20,
        cooldown_seconds=60.0)


def build_report(rows=100_000):
    module, info = native_bridge.load_native()
    frame = synthetic(rows)
    run(module, frame)
    started = perf_counter(); result = run(module, frame)
    elapsed_ms = (perf_counter() - started) * 1000.0
    intents = int(np.count_nonzero(result.kind))
    return migration_baseline.canonicalize({
        'report_schema_version': 1,
        'environment': migration_baseline.environment_record(),
        'runtime': {'native_version': info['native_version'],
                    'python_cache_tag': sys.implementation.cache_tag},
        'scope': {'entry_conditions': len(ENTRY), 'exit_conditions': len(EXIT),
                  'stateful_risk': True, 'broker_calls': 0},
        'performance': {
            'rows': rows, 'native_end_to_end_ms': elapsed_ms,
            'bars_per_second': rows / (elapsed_ms / 1000.0),
            'intents': intents,
            'intent_hash': migration_baseline.stable_hash({
                'kind': result.kind.tolist(), 'action': result.action.tolist(),
                'reason': result.reason.tolist(), 'state': result.state.tolist()}),
        },
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description='ADR-153 native strategy runtime benchmark')
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
