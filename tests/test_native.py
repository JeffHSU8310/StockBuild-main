# -*- coding: utf-8 -*-
"""ADR-145/146 C++/pybind11/SQLite 原生邊界整合測試。"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SANITIZERS = '--sanitizers' in sys.argv

from core import native_bridge
from data import kbars_store
from native import build_native


class TestNativeFoundationADR145ADR146(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_name = 'build-test-asan' if SANITIZERS else 'build-test'
        result = build_native.build(
            ROOT / 'native' / build_name, config='Release', sanitizers=SANITIZERS)
        cls.build_result = result
        spec = importlib.util.spec_from_file_location('_stockbuild_native', result['module'])
        if spec is None or spec.loader is None:
            raise RuntimeError(f"無法建立 native module spec: {result['module']}")
        cls.native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.native)

    def _batch(self, n=10):
        return native_bridge.KBarBatch(
            np.arange(n, dtype=np.int64),
            np.full(n, 1.0, dtype=np.float64),
            np.full(n, 2.0, dtype=np.float64),
            np.full(n, 0.5, dtype=np.float64),
            np.full(n, 1.5, dtype=np.float64),
            np.full(n, 100.0, dtype=np.float64),
            np.zeros(n, dtype=np.uint32),
        )

    def _write_range_fixture(self, db):
        index = pd.date_range('2026-08-01 09:00:00.123456789', periods=6, freq='min')
        frame = pd.DataFrame({
            'Open': np.arange(100.0, 106.0),
            'High': np.arange(101.0, 107.0),
            'Low': np.arange(99.0, 105.0),
            'Close': np.arange(100.5, 106.5),
            'Volume': np.arange(1000.0, 1006.0),
        }, index=index)
        kbars_store.upsert(db, '2330', 'stock', '1分K', frame)
        return frame

    def test_build_uses_official_windows_abi_toolchain(self):
        if os.name == 'nt':
            self.assertIn('MSVC', self.build_result['compiler'])
        self.assertTrue(Path(self.build_result['module']).exists())
        if SANITIZERS:
            self.assertTrue(self.build_result['sanitizers'])
            runtime = Path(self.build_result['asan_runtime'])
            self.assertTrue(runtime.exists())
            self.assertTrue((Path(self.build_result['module']).parent / runtime.name).exists())

    def test_abi_layout_matches_python_contract(self):
        info = native_bridge.validate_abi_info(dict(self.native.abi_info()))
        self.assertEqual(info['struct_size'], 56)
        self.assertEqual(info['offsets']['flags'], 48)
        self.native.handshake(native_bridge.ABI_VERSION, native_bridge.KBAR_SCHEMA_VERSION)

    def test_wrong_abi_or_schema_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self.native.handshake(native_bridge.ABI_VERSION + 1,
                                  native_bridge.KBAR_SCHEMA_VERSION)
        with self.assertRaises(RuntimeError):
            self.native.handshake(native_bridge.ABI_VERSION,
                                  native_bridge.KBAR_SCHEMA_VERSION + 1)

    def test_one_million_rows_cross_in_one_batch_and_echo_is_zero_copy(self):
        batch = self._batch(1_000_000)
        result = dict(self.native.inspect_kbars(*batch.as_args()))
        self.assertEqual(result['rows'], 1_000_000)
        self.assertAlmostEqual(result['checksum'], 105_000_000.0)
        echoed = self.native.echo_kbars(*batch.as_args())
        for original, returned in zip(batch.as_args(), echoed):
            self.assertTrue(np.shares_memory(original, returned))
            self.assertEqual(original.__array_interface__['data'][0],
                             returned.__array_interface__['data'][0])

    def test_noncontiguous_wrong_dtype_and_length_are_rejected(self):
        batch = self._batch(10)
        bad_stride = np.arange(20, dtype=np.float64)[::2]
        with self.assertRaises(ValueError):
            self.native.inspect_kbars(batch.timestamps, bad_stride, batch.high, batch.low,
                                      batch.close, batch.volume, batch.flags)
        with self.assertRaises(ValueError):
            self.native.inspect_kbars(batch.timestamps, batch.open.astype(np.float32),
                                      batch.high, batch.low, batch.close, batch.volume,
                                      batch.flags)
        with self.assertRaises(ValueError):
            self.native.inspect_kbars(batch.timestamps[:-1], batch.open, batch.high,
                                      batch.low, batch.close, batch.volume, batch.flags)

    def test_dataframe_bridge_produces_exact_contiguous_columns(self):
        idx = pd.date_range('2026-08-01', periods=5, freq='min')
        df = pd.DataFrame({'Open': 1.0, 'High': 2.0, 'Low': 0.5,
                           'Close': 1.5, 'Volume': 100.0}, index=idx)
        batch = native_bridge.prepare_kbars(df)
        result = native_bridge.inspect_batch(self.native, batch)
        self.assertEqual(result['rows'], 5)
        self.assertEqual(batch.timestamps.dtype, np.dtype(np.int64))
        self.assertTrue(all(value.flags.c_contiguous for value in batch.as_args()))

    def test_dataframe_bridge_normalizes_timestamp_unit_to_nanoseconds(self):
        # pandas 3 從秒級 ISO 字串建立的 index 可能是 datetime64[us]；ABI 仍須是 ns。
        index = pd.to_datetime(['2024-01-01T00:00:00', '2024-01-01T00:00:01'])
        frame = pd.DataFrame({
            'Open': [1.0, 2.0], 'High': [2.0, 3.0], 'Low': [0.5, 1.5],
            'Close': [1.5, 2.5], 'Volume': [10.0, 20.0],
        }, index=index)
        batch = native_bridge.prepare_kbars(frame)
        np.testing.assert_array_equal(
            batch.timestamps,
            [1_704_067_200_000_000_000, 1_704_067_201_000_000_000])

    def test_module_loader_accepts_exact_version(self):
        old = sys.modules.get('_stockbuild_native')
        sys.modules['_stockbuild_native'] = self.native
        try:
            loaded, info = native_bridge.load_native()
            self.assertIs(loaded, self.native)
            self.assertEqual(info['abi_version'], 1)
        finally:
            if old is None:
                sys.modules.pop('_stockbuild_native', None)
            else:
                sys.modules['_stockbuild_native'] = old

    def test_sqlite_probe_is_readonly_query_only_and_releases_windows_lock(self):
        temp = tempfile.TemporaryDirectory()
        try:
            db = os.path.join(temp.name, 'kbars.sqlite3')
            idx = pd.date_range('2026-08-01', periods=3, freq='min')
            df = pd.DataFrame({'Open': [1, 2, 3], 'High': [2, 3, 4],
                               'Low': [0, 1, 2], 'Close': [1.5, 2.5, 3.5],
                               'Volume': [10, 20, 30]}, index=idx)
            kbars_store.upsert(db, '2330', 'stock', '1分K', df)
            result = native_bridge.probe_sqlite(
                self.native, db, '2330', 'stock', '1分K')
            self.assertTrue(result['readonly'])
            self.assertTrue(result['query_only'])
            self.assertEqual(result['count'], 3)
            self.assertGreaterEqual(result['schema_version'], 1)
            # 若 statement/connection 沒 RAII finalize/close，Windows 這行會 WinError 32。
            os.remove(db)
            self.assertFalse(os.path.exists(db))
        finally:
            temp.cleanup()

    def test_sqlite_probe_never_creates_missing_database(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = os.path.join(temp, 'missing.sqlite3')
            with self.assertRaises(RuntimeError):
                native_bridge.probe_sqlite(self.native, missing, '2330', 'stock', '1分K')
            self.assertFalse(os.path.exists(missing))

    def test_sqlite_prepare_error_still_releases_windows_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            db = os.path.join(temp, 'wrong-schema.sqlite3')
            connection = sqlite3.connect(db)
            try:
                connection.execute('CREATE TABLE unrelated (id INTEGER PRIMARY KEY)')
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(RuntimeError):
                native_bridge.probe_sqlite(self.native, db, '2330', 'stock', '1分K')
            os.remove(db)
            self.assertFalse(os.path.exists(db))

    def test_sqlite_range_matches_python_with_inclusive_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            db = os.path.join(temp, 'range.sqlite3')
            frame = self._write_range_fixture(db)
            start, end = frame.index[1], frame.index[4]
            expected = kbars_store.load(db, '2330', 'stock', '1分K', start, end)
            result = native_bridge.read_sqlite_range(
                self.native, db, '2330', 'stock', '1分K', start, end)

            self.assertEqual(result.rows, 4)
            self.assertTrue(result.readonly)
            self.assertTrue(result.query_only)
            np.testing.assert_array_equal(result.batch.timestamps, expected.index.asi8)
            for native_values, name in zip(
                    result.batch.as_args()[1:6], ('Open', 'High', 'Low', 'Close', 'Volume')):
                np.testing.assert_array_equal(native_values, expected[name].to_numpy(np.float64))
            self.assertTrue(all(not values.flags.writeable for values in result.batch.as_args()))
            self.assertTrue(all(not values.flags.owndata for values in result.batch.as_args()))
            owner = result.batch.timestamps.base
            self.assertIsNotNone(owner)
            self.assertTrue(all(values.base is owner for values in result.batch.as_args()))
            inspected = native_bridge.inspect_batch(self.native, result.batch)
            self.assertEqual(inspected['rows'], 4)
            # buffer 活著時 SQLite 連線也已關閉；Windows 必須能立刻移除 DB。
            os.remove(db)
            self.assertEqual(result.batch.close[-1], expected['Close'].iloc[-1])

    def test_sqlite_range_supports_all_boundary_shapes_and_empty_result(self):
        with tempfile.TemporaryDirectory() as temp:
            db = os.path.join(temp, 'ranges.sqlite3')
            frame = self._write_range_fixture(db)
            cases = (
                (None, None, 6),
                (frame.index[3], None, 3),
                (None, frame.index[2], 3),
                (frame.index[-1] + pd.Timedelta(days=1), None, 0),
            )
            for start, end, expected_rows in cases:
                with self.subTest(start=start, end=end):
                    result = native_bridge.read_sqlite_range(
                        self.native, db, '2330', 'stock', '1分K', start, end)
                    self.assertEqual(result.rows, expected_rows)
                    self.assertEqual(native_bridge.inspect_batch(
                        self.native, result.batch)['rows'], expected_rows)

    def test_sqlite_range_filters_metadata_and_normalizes_timezone(self):
        with tempfile.TemporaryDirectory() as temp:
            db = os.path.join(temp, 'timezone.sqlite3')
            index = pd.date_range('2026-08-01 09:00:00.000000001+08:00', periods=2, freq='s')
            frame = pd.DataFrame({
                'Open': [1.0, 2.0], 'High': [2.0, 3.0], 'Low': [0.5, 1.5],
                'Close': [1.5, 2.5], 'Volume': [10.0, 20.0],
            }, index=index)
            kbars_store.upsert(db, 'TXF', 'future', '1分K', frame)
            kbars_store.upsert(db, '2330', 'stock', '1分K', frame * 10)
            result = native_bridge.read_sqlite_range(
                self.native, db, 'TXF', 'future', '1分K')
            np.testing.assert_array_equal(
                result.batch.timestamps, index.tz_convert('UTC').asi8)
            np.testing.assert_array_equal(result.batch.open, [1.0, 2.0])

    def test_sqlite_range_query_plan_uses_composite_index(self):
        with tempfile.TemporaryDirectory() as temp:
            db = os.path.join(temp, 'plan.sqlite3')
            frame = self._write_range_fixture(db)
            plan = native_bridge.sqlite_range_query_plan(
                self.native, db, '2330', 'stock', '1分K', frame.index[1], frame.index[4])
            self.assertIn('SEARCH kbars USING INDEX', plan)
            self.assertTrue('idx_kbars_lookup' in plan or 'sqlite_autoindex_kbars_1' in plan)
            self.assertNotIn('SCAN kbars', plan)

    def test_sqlite_range_reads_committed_snapshot_while_writer_is_open(self):
        with tempfile.TemporaryDirectory() as temp:
            db = os.path.join(temp, 'snapshot.sqlite3')
            self._write_range_fixture(db)
            writer = sqlite3.connect(db, timeout=30)
            try:
                writer.execute('BEGIN IMMEDIATE')
                writer.execute(
                    "UPDATE kbars SET close=999 WHERE symbol='2330' AND ts=(SELECT MIN(ts) FROM kbars)")
                result = native_bridge.read_sqlite_range(
                    self.native, db, '2330', 'stock', '1分K')
                self.assertEqual(result.batch.close[0], 100.5)
                self.assertNotIn(999.0, result.batch.close)
            finally:
                writer.rollback()
                writer.close()
            os.remove(db)

    def test_sqlite_range_rejects_invalid_input_and_never_creates_missing_db(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = os.path.join(temp, 'missing.sqlite3')
            with self.assertRaises(native_bridge.KBarSchemaError):
                native_bridge.read_sqlite_range(
                    self.native, missing, '2330', 'stock', '1分K',
                    '2026-08-02', '2026-08-01')
            self.assertFalse(os.path.exists(missing))
            with self.assertRaises(RuntimeError):
                native_bridge.read_sqlite_range(
                    self.native, missing, '2330', 'stock', '1分K')
            self.assertFalse(os.path.exists(missing))

    def test_sqlite_range_bad_timestamp_and_schema_release_windows_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            for filename, timestamp in (('bad-time.sqlite3', 'not-a-time'),
                                        ('bad-type.sqlite3', '2026-08-01T09:00:00')):
                db = os.path.join(temp, filename)
                connection = sqlite3.connect(db)
                try:
                    connection.execute(
                        'CREATE TABLE kbars (symbol TEXT, asset_type TEXT, timeframe TEXT, '
                        'ts TEXT, open, high REAL, low REAL, close REAL, volume REAL)')
                    open_value = 'not-a-number' if filename.startswith('bad-type') else 1.0
                    connection.execute(
                        'INSERT INTO kbars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        ('2330', 'stock', '1分K', timestamp, open_value, 2.0, 0.5, 1.5, 10.0))
                    connection.commit()
                finally:
                    connection.close()
                with self.assertRaises(RuntimeError):
                    native_bridge.read_sqlite_range(
                        self.native, db, '2330', 'stock', '1分K')
                os.remove(db)
                self.assertFalse(os.path.exists(db))

    def test_sqlite_range_differential_assertion_detects_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            db = os.path.join(temp, 'mutation.sqlite3')
            expected = self._write_range_fixture(db)
            result = native_bridge.read_sqlite_range(
                self.native, db, '2330', 'stock', '1分K')
            mutated = result.batch.close.copy()
            mutated[2] += 0.01
            with self.assertRaises(AssertionError):
                np.testing.assert_array_equal(mutated, expected['Close'].to_numpy(np.float64))


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]], verbosity=2)
