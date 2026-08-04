# -*- coding: utf-8 -*-
"""ADR-146：SQLite → C++ 欄式 KBar buffer 百萬根基準。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

import numpy as np

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


def _create_database(path, rows):
    kbars_store.initialize(path)
    digits = ','.join(f'({value})' for value in range(10))
    sql = f"""
        WITH digits(d) AS (VALUES {digits}), sequence(i) AS (
            SELECT a.d + b.d*10 + c.d*100 + d.d*1000 + e.d*10000 + f.d*100000
            FROM digits a CROSS JOIN digits b CROSS JOIN digits c
            CROSS JOIN digits d CROSS JOIN digits e CROSS JOIN digits f
        )
        INSERT INTO kbars(symbol, asset_type, timeframe, ts, open, high, low, close, volume)
        SELECT '2330', 'stock', '1分K',
               strftime('%Y-%m-%dT%H:%M:%S', '2024-01-01', '+' || i || ' seconds'),
               i + 1.0, i + 2.0, i + 0.5, i + 1.5, 1000.0 + (i % 100)
        FROM sequence WHERE i < ? ORDER BY i
    """
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        with connection:
            connection.execute('PRAGMA synchronous=OFF')
            connection.execute(sql, (rows,))


def build_report(rows=1_000_000, repeats=3, warmups=1):
    if rows < 1 or rows > 1_000_000:
        raise ValueError('rows 必須介於 1 與 1,000,000')
    built = build_native.build(ROOT / 'native' / 'build-benchmark', config='Release')
    module_path = Path(built['module'])
    module = _load_module(module_path)
    native_bridge.validate_abi_info(dict(module.abi_info()))

    with tempfile.TemporaryDirectory() as temp:
        db = str(Path(temp) / 'kbars.sqlite3')
        _create_database(db, rows)
        plan = native_bridge.sqlite_range_query_plan(
            module, db, '2330', 'stock', '1分K', '2024-01-01', '2024-12-31')

        native_result, native_timing = _measure(
            lambda: native_bridge.read_sqlite_range(
                module, db, '2330', 'stock', '1分K'), repeats, warmups)

        def python_reference():
            frame = kbars_store.load(db, '2330', 'stock', '1分K')
            return native_bridge.prepare_kbars(frame)

        python_batch, python_timing = _measure(python_reference, repeats, warmups)
        for actual, expected in zip(native_result.batch.as_args(), python_batch.as_args()):
            np.testing.assert_array_equal(actual, expected)
        inspected = native_bridge.inspect_batch(module, native_result.batch)
        os_removed_after_read = False
        Path(db).unlink()
        os_removed_after_read = not Path(db).exists()

    native_median = native_timing['median_ms']
    python_median = python_timing['median_ms']
    return migration_baseline.canonicalize({
        'report_schema_version': 1,
        'environment': migration_baseline.environment_record(),
        'compiler': built['compiler'],
        'native_module_sha256': hashlib.sha256(module_path.read_bytes()).hexdigest(),
        'rows': rows,
        'repeats': repeats,
        'warmups': warmups,
        'timings': {
            'native_sqlite_to_columns': native_timing,
            'python_sqlite_to_pandas_to_columns': python_timing,
        },
        'result': {
            'speedup_vs_python': python_median / native_median,
            'native_rows_per_second': rows / (native_median / 1000.0),
            'rows': native_result.rows,
            'first_timestamp_ns': inspected['first_timestamp_ns'],
            'last_timestamp_ns': inspected['last_timestamp_ns'],
            'checksum': inspected['checksum'],
            'all_columns_zero_copy_views': all(
                not value.flags.owndata for value in native_result.batch.as_args()),
            'all_columns_readonly': all(
                not value.flags.writeable for value in native_result.batch.as_args()),
            'query_plan': plan,
            'database_removed_after_read': os_removed_after_read,
        },
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description='ADR-146 SQLite range buffer benchmark')
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
