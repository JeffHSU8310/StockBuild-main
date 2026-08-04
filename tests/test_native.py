# -*- coding: utf-8 -*-
"""ADR-145～150 C++/pybind11/SQLite/產品 runtime 整合測試。"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SANITIZERS = '--sanitizers' in sys.argv

from core import engine_router, futures_session, indicators, jae, native_bridge
from data import kbars_store
from native import build_native


class TestNativeFoundationADR145ToADR150(unittest.TestCase):
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

    def _ohlcv(self, index):
        rows = len(index)
        base = np.arange(rows, dtype=np.float64) + 100.0
        return pd.DataFrame({
            'Open': base,
            'High': base + 2.0,
            'Low': base - 1.0,
            'Close': base + 0.5,
            'Volume': np.arange(rows, dtype=np.float64) + 10.0,
        }, index=index)

    def _assert_batch_matches_frame(self, batch, frame, tolerance=0.0):
        expected_timestamps = pd.DatetimeIndex(frame.index).as_unit('ns').asi8
        np.testing.assert_array_equal(batch.timestamps, expected_timestamps)
        for actual, name in zip(
                batch.as_args()[1:6], ('Open', 'High', 'Low', 'Close', 'Volume')):
            np.testing.assert_allclose(
                actual, frame[name].to_numpy(np.float64), rtol=tolerance,
                atol=tolerance, equal_nan=True)

    def _advanced_python(self, frame, kdj=(9, 3, 3), dmi_n=14,
                         jae=(14, 9, 3, 3, 60)):
        def kdj_values(n, m1, m2):
            low_min = frame['Low'].rolling(n).min()
            high_max = frame['High'].rolling(n).max()
            rsv = 100 * (frame['Close'] - low_min) / (high_max - low_min)
            k = rsv.ewm(com=m1 - 1, adjust=False).mean()
            d = k.ewm(com=m2 - 1, adjust=False).mean()
            return rsv, k, d, 3 * k - 2 * d

        rsv, k, d, j = kdj_values(*kdj)
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
            (frame['High'] - frame['Close'].shift(1)).abs(),
            (frame['Low'] - frame['Close'].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(span=dmi_n, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(span=dmi_n, adjust=False).mean() / atr
        minus_di = 100 * minus_dm.ewm(span=dmi_n, adjust=False).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
        adx = dx.ewm(span=dmi_n, adjust=False).mean()

        a_period, j_n, j_m1, j_m2, e_period = jae
        delta = frame['Close'].diff()
        gain = delta.clip(lower=0).ewm(com=a_period - 1, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(com=a_period - 1, adjust=False).mean()
        jae_a = 100 - 100 / (1 + gain / loss)
        jae_j = kdj_values(j_n, j_m1, j_m2)[3]
        jae_e = jae_a.ewm(span=e_period, adjust=False).mean()
        return {
            'rsv': rsv, 'k': k, 'd': d, 'j': j,
            'plus_dm': plus_dm, 'minus_dm': minus_dm,
            'plus_di': plus_di, 'minus_di': minus_di, 'adx': adx,
            'jae_a': jae_a, 'jae_j': jae_j, 'jae_e': jae_e,
        }

    def test_build_uses_official_windows_abi_toolchain(self):
        if os.name == 'nt':
            self.assertIn('MSVC', self.build_result['compiler'])
        self.assertTrue(Path(self.build_result['module']).exists())
        if SANITIZERS:
            self.assertTrue(self.build_result['sanitizers'])
            runtime = Path(self.build_result['asan_runtime'])
            self.assertTrue(runtime.exists())
            self.assertTrue((Path(self.build_result['module']).parent / runtime.name).exists())

    def test_adr150_install_and_product_loader_use_abi_scoped_runtime(self):
        install_root = ROOT / 'native' / ('build-test-runtime-asan' if SANITIZERS
                                          else 'build-test-runtime')
        if SANITIZERS:
            with self.assertRaisesRegex(ValueError, '不可安裝'):
                build_native.install(self.build_result, install_root)
            return

        installed = build_native.install(self.build_result, install_root)
        module_path = Path(installed['installed_module'])
        manifest_path = Path(installed['manifest'])
        self.assertEqual(module_path.parent.name, sys.implementation.cache_tag)
        self.assertTrue(module_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        self.assertEqual(manifest['module'], module_path.name)
        self.assertEqual(manifest['python_cache_tag'], sys.implementation.cache_tag)

        probe = subprocess.run(
            [sys.executable, '-c',
             ('import json; from core import native_bridge; '
              f'm, info = native_bridge.load_native(search_dirs=[{str(module_path.parent)!r}]); '
              'print(json.dumps(info))')],
            cwd=ROOT, check=True, capture_output=True, text=True)
        info = json.loads(probe.stdout)
        self.assertEqual(Path(info['module_file']), module_path.resolve())
        self.assertEqual(info['native_version'], '0.4.0')

    def test_abi_layout_matches_python_contract(self):
        info = native_bridge.validate_abi_info(dict(self.native.abi_info()))
        self.assertEqual(info['native_version'], '0.4.0')
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

    def test_fixed_resampler_matches_pandas_and_aggregates_flags(self):
        index = pd.date_range('2026-08-03 09:01', periods=12, freq='min')
        frame = self._ohlcv(index)
        flags = np.arange(1, len(frame) + 1, dtype=np.uint32)
        source = native_bridge.prepare_kbars(frame, flags=flags)
        result = native_bridge.resample_kbars(
            self.native, source, 'fixed', interval_minutes=5)
        expected = frame.resample('5min', label='left', closed='left').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min',
            'Close': 'last', 'Volume': 'sum',
        }).dropna()
        self._assert_batch_matches_frame(result, expected)
        expected_flags = []
        flag_series = pd.Series(flags, index=index)
        for _, values in flag_series.resample('5min', label='left', closed='left'):
            if len(values):
                expected_flags.append(np.bitwise_or.reduce(values.to_numpy(np.uint32)))
        np.testing.assert_array_equal(result.flags, expected_flags)
        self.assertTrue(all(not column.flags.writeable for column in result.as_args()))
        self.assertTrue(all(not column.flags.owndata for column in result.as_args()))
        owner = result.timestamps.base
        self.assertTrue(all(column.base is owner for column in result.as_args()))

    def test_calendar_resampler_matches_pandas_with_explicit_timezone(self):
        local_index = pd.to_datetime([
            '2026-07-31 09:00+08:00', '2026-07-31 13:30+08:00',
            '2026-08-03 09:00+08:00', '2026-08-03 13:30+08:00',
            '2026-08-10 09:00+08:00', '2026-08-10 13:30+08:00',
        ], format='mixed')
        frame = self._ohlcv(local_index)
        source = native_bridge.prepare_kbars(frame)
        rules = {'day': 'D', 'week': 'W-MON', 'month': 'MS'}
        for mode, rule in rules.items():
            with self.subTest(mode=mode):
                result = native_bridge.resample_kbars(
                    self.native, source, mode, timezone_offset_minutes=480)
                expected = frame.resample(rule, label='left', closed='left').agg({
                    'Open': 'first', 'High': 'max', 'Low': 'min',
                    'Close': 'last', 'Volume': 'sum',
                }).dropna()
                self._assert_batch_matches_frame(result, expected)

    def test_future_session_resampler_matches_all_and_day_python_contracts(self):
        session_dates = pd.to_datetime([
            '2026-07-31', '2026-08-03', '2026-08-04', '2026-08-10'])
        timestamps = []
        for session_date in session_dates:
            timestamps.extend([
                session_date - pd.Timedelta(days=1) + pd.Timedelta(hours=15),
                session_date + pd.Timedelta(hours=4, minutes=59),
                session_date + pd.Timedelta(hours=8, minutes=45),
                session_date + pd.Timedelta(hours=13, minutes=45),
            ])
        index = pd.DatetimeIndex(sorted(timestamps))
        frame = self._ohlcv(index)
        source = native_bridge.prepare_kbars(frame)
        agg = {'Open': 'first', 'High': 'max', 'Low': 'min',
               'Close': 'last', 'Volume': 'sum'}
        modes = {'future_day': '日K', 'future_week': '周K', 'future_month': '月K'}
        for basis in ('all', 'day'):
            for mode, timeframe in modes.items():
                with self.subTest(basis=basis, mode=mode):
                    result = native_bridge.resample_kbars(
                        self.native, source, mode, session_basis=basis)
                    expected = futures_session.resample_future_session(
                        frame, timeframe, agg, session_basis=basis)
                    self._assert_batch_matches_frame(result, expected)

    def test_future_session_1345_boundary_and_1346_shift_match_python(self):
        index = pd.to_datetime([
            '2026-08-03 13:45:59', '2026-08-03 13:46:00',
            '2026-08-03 15:00:00', '2026-08-04 08:45:00',
            '2026-08-04 13:45:00'])
        frame = self._ohlcv(index)
        source = native_bridge.prepare_kbars(frame)
        result = native_bridge.resample_kbars(self.native, source, 'future_day')
        expected = futures_session.resample_future_session(
            frame, '日K', {'Open': 'first', 'High': 'max', 'Low': 'min',
                           'Close': 'last', 'Volume': 'sum'})
        self._assert_batch_matches_frame(result, expected)
        self.assertEqual(result.rows, 2)

    def test_indicator_core_matches_independent_pandas_formulas(self):
        rows = 240
        index = pd.date_range('2026-01-01', periods=rows, freq='D')
        x = np.arange(rows, dtype=np.float64)
        close = 100.0 + x * 0.07 + np.sin(x / 4.0) * 3.0 + np.cos(x / 11.0)
        frame = self._ohlcv(index)
        frame['Close'] = close
        batch = native_bridge.prepare_kbars(frame)
        result = native_bridge.calculate_native_indicators(
            self.native, batch, ma_period=20, rsi_period=14,
            macd_fast=12, macd_slow=26, macd_signal=9,
            bb_period=20, bb_std_up=2.25, bb_std_down=1.75, bb_ma_type='SMA')

        series = pd.Series(close, index=index)
        expected_sma = series.rolling(20).mean()
        expected_ema = series.ewm(span=20, adjust=False).mean()
        weights = np.arange(1, 21, dtype=np.float64)
        expected_wma = series.rolling(20).apply(
            lambda values: np.dot(values, weights) / weights.sum(), raw=True)
        delta = series.diff()
        gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
        expected_rsi = 100 - 100 / (1 + gain / loss)
        fast = series.ewm(span=12, adjust=False).mean()
        slow = series.ewm(span=26, adjust=False).mean()
        expected_macd = fast - slow
        expected_signal = expected_macd.ewm(span=9, adjust=False).mean()
        expected_std = series.rolling(20).std()
        expected = {
            'sma': expected_sma,
            'ema': expected_ema,
            'wma': expected_wma,
            'rsi': expected_rsi,
            'macd': expected_macd,
            'signal': expected_signal,
            'hist': expected_macd - expected_signal,
            'bb_mid': expected_sma,
            'bb_std': expected_std,
            'bb_upper': expected_sma + 2.25 * expected_std,
            'bb_lower': expected_sma - 1.75 * expected_std,
            'bb_width': ((4.0 * expected_std) / expected_sma) * 100,
        }
        for name, actual in result.as_dict().items():
            np.testing.assert_allclose(
                actual, expected[name].to_numpy(np.float64),
                rtol=2e-11, atol=2e-11, equal_nan=True, err_msg=name)
        self.assertTrue(all(not column.flags.writeable
                            for column in result.as_dict().values()))
        owner = result.sma.base
        self.assertTrue(all(column.base is owner for column in result.as_dict().values()))

    def test_bollinger_mid_supports_ema_and_wma(self):
        index = pd.date_range('2026-01-01', periods=80, freq='D')
        frame = self._ohlcv(index)
        batch = native_bridge.prepare_kbars(frame)
        series = frame['Close']
        weights = np.arange(1, 11, dtype=np.float64)
        expected = {
            'EMA': series.ewm(span=10, adjust=False).mean(),
            'WMA': series.rolling(10).apply(
                lambda values: np.dot(values, weights) / weights.sum(), raw=True),
        }
        for kind in ('EMA', 'WMA'):
            with self.subTest(kind=kind):
                result = native_bridge.calculate_native_indicators(
                    self.native, batch, ma_period=10, bb_period=10, bb_ma_type=kind)
                np.testing.assert_allclose(
                    result.bb_mid, expected[kind].to_numpy(np.float64),
                    rtol=1e-12, atol=1e-12, equal_nan=True)

    def test_native_compute_empty_short_and_invalid_inputs(self):
        empty = self._batch(0)
        resampled = native_bridge.resample_kbars(
            self.native, empty, 'fixed', interval_minutes=5)
        indicators = native_bridge.calculate_native_indicators(self.native, empty)
        self.assertEqual(resampled.rows, 0)
        self.assertEqual(indicators.rows, 0)

        short = self._batch(3)
        short_indicators = native_bridge.calculate_native_indicators(
            self.native, short, ma_period=5, bb_period=5)
        self.assertTrue(np.isnan(short_indicators.sma).all())
        self.assertTrue(np.isnan(short_indicators.wma).all())
        self.assertTrue(np.isnan(short_indicators.bb_std).all())
        self.assertFalse(np.isnan(short_indicators.ema).any())

        duplicate = self._batch(3)
        duplicate.timestamps[1] = duplicate.timestamps[0]
        with self.assertRaises(ValueError):
            native_bridge.resample_kbars(
                self.native, duplicate, 'fixed', interval_minutes=5)
        descending = self._batch(3)
        descending.timestamps[2] = descending.timestamps[1] - 1
        with self.assertRaises(ValueError):
            native_bridge.resample_kbars(
                self.native, descending, 'fixed', interval_minutes=5)
        nonfinite = self._batch(3)
        nonfinite.close[1] = np.nan
        with self.assertRaises(ValueError):
            native_bridge.resample_kbars(
                self.native, nonfinite, 'fixed', interval_minutes=5)
        with self.assertRaises(ValueError):
            native_bridge.calculate_native_indicators(self.native, nonfinite)
        with self.assertRaises(native_bridge.KBarSchemaError):
            native_bridge.resample_kbars(self.native, short, 'unknown')
        with self.assertRaises(native_bridge.KBarSchemaError):
            native_bridge.resample_kbars(
                self.native, short, 'future_day', session_basis='invalid')
        with self.assertRaises(ValueError):
            self.native.resample_kbars(
                *empty.as_args(), 'future_day', 0, 0, 'invalid')
        with self.assertRaises(native_bridge.KBarSchemaError):
            native_bridge.calculate_native_indicators(
                self.native, short, ma_period=0)

    def test_indicator_differential_assertion_detects_mutation(self):
        index = pd.date_range('2026-01-01', periods=50, freq='D')
        frame = self._ohlcv(index)
        batch = native_bridge.prepare_kbars(frame)
        result = native_bridge.calculate_native_indicators(
            self.native, batch, ma_period=5)
        expected = frame['Close'].rolling(5).mean().to_numpy(np.float64)
        mutated = result.sma.copy()
        mutated[10] += 0.01
        with self.assertRaises(AssertionError):
            np.testing.assert_allclose(mutated, expected, equal_nan=True)

    def test_advanced_indicators_match_independent_pandas_formulas(self):
        rows = 480
        index = pd.date_range('2024-01-01', periods=rows, freq='D')
        x = np.arange(rows, dtype=np.float64)
        close = 120.0 + x * 0.025 + np.sin(x / 5.0) * 4.0 + np.cos(x / 17.0)
        frame = pd.DataFrame({
            'Open': close - 0.2,
            'High': close + 1.0 + (x % 3) * 0.1,
            'Low': close - 0.9 - (x % 5) * 0.08,
            'Close': close,
            'Volume': 1000.0 + x,
        }, index=index)
        batch = native_bridge.prepare_kbars(frame)
        result = native_bridge.calculate_native_advanced_indicators(
            self.native, batch, kdj_n=11, kdj_m1=4, kdj_m2=5, dmi_n=17,
            jae_a_period=13, jae_j_n=10, jae_j_m1=2, jae_j_m2=4,
            jae_e_period=55)
        expected = self._advanced_python(
            frame, kdj=(11, 4, 5), dmi_n=17, jae=(13, 10, 2, 4, 55))
        for name, actual in result.as_dict().items():
            np.testing.assert_allclose(
                actual, expected[name].to_numpy(np.float64),
                rtol=2e-11, atol=2e-11, equal_nan=True, err_msg=name)
        self.assertTrue(all(not column.flags.writeable
                            for column in result.as_dict().values()))
        owner = result.rsv.base
        self.assertTrue(all(column.base is owner for column in result.as_dict().values()))

    def test_advanced_indicators_preserve_zero_range_and_nan_ewm_semantics(self):
        close = np.array([100.0] * 12 + [101.0, 100.5, 102.0, 101.5, 103.0])
        high = np.array([100.0] * 12 + [102.0, 101.0, 103.0, 102.0, 104.0])
        low = np.array([100.0] * 12 + [100.0, 99.5, 101.0, 100.5, 102.0])
        frame = pd.DataFrame({
            'Open': close, 'High': high, 'Low': low, 'Close': close,
            'Volume': np.ones(close.size),
        }, index=pd.date_range('2026-01-01', periods=close.size, freq='D'))
        result = native_bridge.calculate_native_advanced_indicators(
            self.native, native_bridge.prepare_kbars(frame),
            kdj_n=3, jae_j_n=3, jae_e_period=5)
        expected = self._advanced_python(frame, kdj=(3, 3, 3), jae=(14, 3, 3, 3, 5))
        for name, actual in result.as_dict().items():
            np.testing.assert_allclose(
                actual, expected[name].to_numpy(np.float64),
                rtol=2e-12, atol=2e-12, equal_nan=True, err_msg=name)
        self.assertTrue(np.isnan(result.rsv[2:12]).all())
        self.assertTrue(np.isfinite(result.k[12:]).all())

    def test_advanced_indicators_empty_invalid_and_mutation_guards(self):
        empty = self._batch(0)
        result = native_bridge.calculate_native_advanced_indicators(self.native, empty)
        self.assertEqual(result.rows, 0)
        with self.assertRaises(native_bridge.KBarSchemaError):
            native_bridge.calculate_native_advanced_indicators(
                self.native, self._batch(4), kdj_n=0)
        bad = self._batch(4)
        bad.high[2] = np.inf
        with self.assertRaises(ValueError):
            native_bridge.calculate_native_advanced_indicators(self.native, bad)

        frame = self._ohlcv(pd.date_range('2026-01-01', periods=40, freq='D'))
        expected = self._advanced_python(frame)['j'].to_numpy(np.float64)
        actual = native_bridge.calculate_native_advanced_indicators(
            self.native, native_bridge.prepare_kbars(frame)).j.copy()
        actual[20] += 0.1
        with self.assertRaises(AssertionError):
            np.testing.assert_allclose(actual, expected, equal_nan=True)

    def test_adr149_real_native_shadow_routes_shared_indicator_columns(self):
        frame = self._ohlcv(pd.date_range('2024-01-01', periods=480, freq='D'))
        settings = {
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
        expected = indicators.calculate_indicators(
            frame, [False] * 6, ['SMA'] * 6, [5] * 6,
            bb_show=True, bbw_show=True, bb_period=20,
            bb_std_up=2.1, bb_std_dn=1.8, bb_type='EMA',
            bb2_show=True, bb2_period=55, bb2_std_up=2.5,
            bb2_std_dn=2.2, bb2_type='WMA',
            macd_show=True, macd_f=10, macd_s=24, macd_sig=7,
            rsi_show=True, rsi_p=13,
            kdj_show=True, kd_n=11, kd_m1=4, kd_m2=5,
            dmi_show=True, dmi_n=17)
        jae.compute(expected, settings['jae_params'])

        def provider(source, route_settings):
            return engine_router.calculate_native_columns(
                source, route_settings, module=self.native)

        routed = engine_router.route_indicators(
            frame, expected, settings, mode='shadow', native_provider=provider)
        self.assertIs(routed.frame, expected)
        self.assertEqual(routed.telemetry['status'], 'passed')
        self.assertEqual(routed.telemetry['compared_columns'], 25)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]], verbosity=2)
