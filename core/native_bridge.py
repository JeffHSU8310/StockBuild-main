# -*- coding: utf-8 -*-
"""ADR-145/146：Python 與 StockBuild C++ core 的版本／欄式資料邊界。

本檔只驗證 ABI、dtype、stride、SQLite range 與轉送批次資料，不包含策略、
成交或風控規則。
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ABI_VERSION = 1
KBAR_SCHEMA_VERSION = 1
KBAR_STRUCT_SIZE = 56
KBAR_STRUCT_ALIGNMENT = 8
KBAR_OFFSETS = {
    'timestamp_ns': 0, 'open': 8, 'high': 16, 'low': 24,
    'close': 32, 'volume': 40, 'flags': 48, 'reserved': 52,
}
KBAR_DTYPES = {
    'timestamp_ns': 'int64', 'open': 'float64', 'high': 'float64',
    'low': 'float64', 'close': 'float64', 'volume': 'float64',
    'flags': 'uint32', 'reserved': 'uint32',
}


class NativeUnavailable(RuntimeError):
    pass


class NativeVersionError(RuntimeError):
    pass


class KBarSchemaError(ValueError):
    pass


@dataclass(frozen=True)
class KBarBatch:
    timestamps: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    flags: np.ndarray

    @property
    def rows(self):
        return int(self.timestamps.size)

    def as_args(self):
        return (self.timestamps, self.open, self.high, self.low,
                self.close, self.volume, self.flags)


@dataclass(frozen=True)
class SqliteRangeResult:
    batch: KBarBatch
    readonly: bool
    query_only: bool
    schema_version: int
    data_version: int

    @property
    def rows(self):
        return self.batch.rows


def expected_abi_info():
    return {
        'abi_version': ABI_VERSION,
        'schema_version': KBAR_SCHEMA_VERSION,
        'struct_size': KBAR_STRUCT_SIZE,
        'struct_alignment': KBAR_STRUCT_ALIGNMENT,
        'offsets': dict(KBAR_OFFSETS),
        'dtypes': dict(KBAR_DTYPES),
    }


def validate_abi_info(info):
    if not isinstance(info, dict):
        raise NativeVersionError('native abi_info() 必須回傳 dict')
    expected = expected_abi_info()
    for key in ('abi_version', 'schema_version', 'struct_size', 'struct_alignment'):
        if info.get(key) != expected[key]:
            raise NativeVersionError(
                f"native {key} 不相容：expected={expected[key]!r}, actual={info.get(key)!r}")
    for group in ('offsets', 'dtypes'):
        actual = info.get(group)
        if actual != expected[group]:
            raise NativeVersionError(
                f"native {group} 不相容：expected={expected[group]!r}, actual={actual!r}")
    return info


def load_native(module_name='_stockbuild_native'):
    try:
        module = importlib.import_module(module_name)
    except (ImportError, OSError) as exc:
        raise NativeUnavailable(f'無法載入 {module_name}: {exc}') from exc
    try:
        info = validate_abi_info(dict(module.abi_info()))
        module.handshake(ABI_VERSION, KBAR_SCHEMA_VERSION)
    except NativeVersionError:
        raise
    except Exception as exc:
        raise NativeVersionError(f'native 版本握手失敗: {exc}') from exc
    return module, info


def _float_column(df, name):
    try:
        values = pd.to_numeric(df[name], errors='raise').to_numpy(dtype=np.float64, copy=False)
    except (KeyError, TypeError, ValueError) as exc:
        raise KBarSchemaError(f'KBar 缺少或無法轉換 {name} 欄位: {exc}') from exc
    return np.ascontiguousarray(values, dtype=np.float64)


def prepare_kbars(df, flags=None):
    if df is None or not isinstance(df, pd.DataFrame):
        raise KBarSchemaError('KBar 輸入必須是 pandas DataFrame')
    try:
        index = pd.DatetimeIndex(df.index)
    except Exception as exc:
        raise KBarSchemaError(f'KBar index 必須可轉為 DatetimeIndex: {exc}') from exc
    if index.hasnans:
        raise KBarSchemaError('KBar timestamp 不可包含 NaT')
    if index.tz is not None:
        index = index.tz_convert('UTC').tz_localize(None)
    try:
        # pandas 3 會保留 s/ms/us 原始解析度；ABI v1 明確要求 nanoseconds，
        # 不可直接使用未正規化的 asi8，否則同一時間可能相差 1,000 倍。
        timestamps = np.ascontiguousarray(index.as_unit('ns').asi8, dtype=np.int64)
    except (OverflowError, ValueError) as exc:
        raise KBarSchemaError(f'KBar timestamp 無法表示為 int64 nanoseconds: {exc}') from exc
    columns = [_float_column(df, name) for name in ('Open', 'High', 'Low', 'Close', 'Volume')]
    if flags is None:
        flag_values = np.zeros(len(df), dtype=np.uint32)
    else:
        flag_values = np.ascontiguousarray(flags, dtype=np.uint32)
    if flag_values.ndim != 1 or flag_values.size != len(df):
        raise KBarSchemaError('flags 必須是一維且列數與 KBar 相同')
    batch = KBarBatch(timestamps, *columns, flag_values)
    validate_batch(batch)
    return batch


def validate_batch(batch):
    if not isinstance(batch, KBarBatch):
        raise KBarSchemaError('batch 必須是 KBarBatch')
    expected = (
        ('timestamps', np.dtype(np.int64)), ('open', np.dtype(np.float64)),
        ('high', np.dtype(np.float64)), ('low', np.dtype(np.float64)),
        ('close', np.dtype(np.float64)), ('volume', np.dtype(np.float64)),
        ('flags', np.dtype(np.uint32)),
    )
    rows = batch.rows
    for name, dtype in expected:
        value = getattr(batch, name)
        if not isinstance(value, np.ndarray) or value.ndim != 1:
            raise KBarSchemaError(f'{name} 必須是一維 NumPy array')
        if value.dtype != dtype:
            raise KBarSchemaError(f'{name} dtype 必須是 {dtype.name}')
        if not value.flags.c_contiguous:
            raise KBarSchemaError(f'{name} 必須是 C-contiguous')
        if value.size != rows:
            raise KBarSchemaError(f'{name} 列數與 timestamps 不一致')
    return batch


def inspect_batch(module, batch):
    validate_abi_info(dict(module.abi_info()))
    validate_batch(batch)
    return dict(module.inspect_kbars(*batch.as_args()))


def sqlite_library_path():
    if sys.platform == 'win32':
        candidate = Path(sys.executable).resolve().parent / 'DLLs' / 'sqlite3.dll'
        if candidate.exists():
            return str(candidate)
        raise NativeUnavailable(f'找不到 Python SQLite DLL: {candidate}')
    import ctypes.util
    candidate = ctypes.util.find_library('sqlite3')
    if candidate:
        return candidate
    raise NativeUnavailable('找不到系統 SQLite shared library')


def probe_sqlite(module, database_path, symbol, asset_type, timeframe,
                 library_path=None):
    validate_abi_info(dict(module.abi_info()))
    path = str(Path(database_path).resolve())
    lib = library_path or sqlite_library_path()
    return dict(module.sqlite_probe(path, lib, str(symbol), str(asset_type), str(timeframe)))


def _range_boundary(value, name):
    if value is None:
        return None, None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise KBarSchemaError(f'{name} 必須可轉為 Timestamp: {exc}') from exc
    if pd.isna(timestamp):
        raise KBarSchemaError(f'{name} 不可為 NaT')
    return timestamp, timestamp.isoformat()


def _range_call_args(database_path, symbol, asset_type, timeframe,
                     start=None, end=None, library_path=None):
    path = str(Path(database_path).resolve())
    lib = library_path or sqlite_library_path()
    start_value, start_text = _range_boundary(start, 'start')
    end_value, end_text = _range_boundary(end, 'end')
    if start_value is not None and end_value is not None:
        try:
            reversed_range = start_value > end_value
        except TypeError as exc:
            raise KBarSchemaError('start 與 end 必須使用相容的時區') from exc
        if reversed_range:
            raise KBarSchemaError('start 不可晚於 end')
    return (path, lib, str(symbol), str(asset_type), str(timeframe),
            start_text, end_text)


def read_sqlite_range(module, database_path, symbol, asset_type, timeframe,
                      start=None, end=None, library_path=None):
    """以 C++ prepared query 直接建立唯讀欄式 KBar buffers。"""
    validate_abi_info(dict(module.abi_info()))
    payload = dict(module.sqlite_range(*_range_call_args(
        database_path, symbol, asset_type, timeframe, start, end, library_path)))
    try:
        batch = KBarBatch(
            payload['timestamps'], payload['open'], payload['high'], payload['low'],
            payload['close'], payload['volume'], payload['flags'])
        validate_batch(batch)
        if int(payload['rows']) != batch.rows:
            raise KBarSchemaError('native rows metadata 與 KBar buffer 列數不一致')
        if not bool(payload['readonly']) or not bool(payload['query_only']):
            raise KBarSchemaError('native SQLite reader 未維持 readonly/query_only')
        for column in batch.as_args():
            column.setflags(write=False)
        return SqliteRangeResult(
            batch=batch,
            readonly=bool(payload['readonly']),
            query_only=bool(payload['query_only']),
            schema_version=int(payload['schema_version']),
            data_version=int(payload['data_version']),
        )
    except KeyError as exc:
        raise KBarSchemaError(f'native SQLite range 缺少欄位: {exc}') from exc


def sqlite_range_query_plan(module, database_path, symbol, asset_type, timeframe,
                            start=None, end=None, library_path=None):
    validate_abi_info(dict(module.abi_info()))
    return str(module.sqlite_range_query_plan(*_range_call_args(
        database_path, symbol, asset_type, timeframe, start, end, library_path)))
