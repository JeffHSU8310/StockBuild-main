# -*- coding: utf-8 -*-
"""ADR-145：1,000,000 根 KBar 批次邊界與 SQLite probe 基準。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import statistics
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import migration_baseline, native_bridge
from data import kbars_store
from native import build_native


def _load(path):
    spec = importlib.util.spec_from_file_location('_stockbuild_native', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'無法載入 native module: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _measure(function, repeats=5, warmups=2):
    for _ in range(warmups):
        function()
    samples = []
    last = None
    for _ in range(repeats):
        start = time.perf_counter()
        last = function()
        samples.append((time.perf_counter() - start) * 1000.0)
    return last, {'samples_ms': samples, 'median_ms': statistics.median(samples),
                  'min_ms': min(samples), 'max_ms': max(samples)}


def build_report(rows=1_000_000):
    built = build_native.build(ROOT / 'native' / 'build-benchmark', config='Release')
    module_path = Path(built['module'])
    module = _load(module_path)
    native_bridge.validate_abi_info(dict(module.abi_info()))

    idx = pd.date_range('2024-01-01', periods=rows, freq='s')
    values = np.arange(rows, dtype=np.float64)
    frame = pd.DataFrame({'Open': values + 1, 'High': values + 2,
                          'Low': values, 'Close': values + 1.5,
                          'Volume': np.full(rows, 100.0)}, index=idx)
    batch, prepare_time = _measure(lambda: native_bridge.prepare_kbars(frame))
    inspected, inspect_time = _measure(lambda: module.inspect_kbars(*batch.as_args()))
    echoed, echo_time = _measure(lambda: module.echo_kbars(*batch.as_args()))

    with tempfile.TemporaryDirectory() as temp:
        db = str(Path(temp) / 'kbars.sqlite3')
        kbars_store.upsert(db, '2330', 'stock', '1分K', frame.iloc[:100])
        probe, sqlite_time = _measure(
            lambda: native_bridge.probe_sqlite(module, db, '2330', 'stock', '1分K'))

    shares = [np.shares_memory(a, b) for a, b in zip(batch.as_args(), echoed)]
    digest = hashlib.sha256(module_path.read_bytes()).hexdigest()
    return migration_baseline.canonicalize({
        'report_schema_version': 1,
        'environment': migration_baseline.environment_record(),
        'compiler': built['compiler'],
        'native_module_sha256': digest,
        'abi': dict(module.abi_info()),
        'rows': rows,
        'timings': {'dataframe_to_columns': prepare_time,
                    'cpp_inspect_scan': inspect_time,
                    'zero_copy_echo': echo_time,
                    'sqlite_readonly_probe': sqlite_time},
        'result': {'inspected_rows': inspected['rows'],
                   'checksum': inspected['checksum'],
                   'all_echo_arrays_share_memory': all(shares),
                   'sqlite_readonly': probe['readonly'],
                   'sqlite_query_only': probe['query_only'],
                   'sqlite_count': probe['count']},
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description='ADR-145 native boundary benchmark')
    parser.add_argument('--rows', type=int, default=1_000_000)
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    payload = build_report(args.rows)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        Path(args.output).write_text(rendered, encoding='utf-8')
    print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
