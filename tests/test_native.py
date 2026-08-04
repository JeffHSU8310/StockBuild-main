# -*- coding: utf-8 -*-
"""ADR-145 C++/pybind11/SQLite 原生邊界整合測試。"""
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

from core import native_bridge
from data import kbars_store
from native import build_native


class TestNativeFoundationADR145(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = build_native.build(ROOT / 'native' / 'build-test', config='Release')
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

    def test_build_uses_official_windows_abi_toolchain(self):
        if os.name == 'nt':
            self.assertIn('MSVC', self.build_result['compiler'])
        self.assertTrue(Path(self.build_result['module']).exists())

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
