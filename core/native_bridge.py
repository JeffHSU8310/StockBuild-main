# -*- coding: utf-8 -*-
"""ADR-145～152：Python 與 StockBuild C++ core 的版本／欄式資料邊界。

本檔只驗證 ABI、dtype、stride、SQLite range 與轉送批次資料，不包含策略、
成交或風控規則。
"""
from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
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


@dataclass(frozen=True)
class IndicatorBatch:
    sma: np.ndarray
    ema: np.ndarray
    wma: np.ndarray
    rsi: np.ndarray
    macd: np.ndarray
    signal: np.ndarray
    hist: np.ndarray
    bb_mid: np.ndarray
    bb_std: np.ndarray
    bb_upper: np.ndarray
    bb_lower: np.ndarray
    bb_width: np.ndarray

    @property
    def rows(self):
        return int(self.sma.size)

    def as_dict(self):
        return {name: getattr(self, name) for name in (
            'sma', 'ema', 'wma', 'rsi', 'macd', 'signal', 'hist',
            'bb_mid', 'bb_std', 'bb_upper', 'bb_lower', 'bb_width')}


@dataclass(frozen=True)
class MultiMovingAverageBatch:
    values: tuple[np.ndarray, ...]

    @property
    def rows(self):
        return int(self.values[0].size) if self.values else 0

    @property
    def count(self):
        return len(self.values)


@dataclass(frozen=True)
class ConditionBatch:
    values: tuple[np.ndarray, ...]

    @property
    def rows(self):
        return int(self.values[0].size) if self.values else 0

    @property
    def count(self):
        return len(self.values)


@dataclass(frozen=True)
class AdvancedIndicatorBatch:
    rsv: np.ndarray
    k: np.ndarray
    d: np.ndarray
    j: np.ndarray
    plus_dm: np.ndarray
    minus_dm: np.ndarray
    plus_di: np.ndarray
    minus_di: np.ndarray
    adx: np.ndarray
    jae_a: np.ndarray
    jae_j: np.ndarray
    jae_e: np.ndarray

    @property
    def rows(self):
        return int(self.rsv.size)

    def as_dict(self):
        return {name: getattr(self, name) for name in (
            'rsv', 'k', 'd', 'j', 'plus_dm', 'minus_dm',
            'plus_di', 'minus_di', 'adx', 'jae_a', 'jae_j', 'jae_e')}


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


def native_runtime_dirs():
    """回傳受控的產品 extension 搜尋目錄，不掃描任意 build／cwd。"""
    cache_tag = sys.implementation.cache_tag or 'python'
    project_root = Path(__file__).resolve().parents[1]
    candidates = []
    configured = os.environ.get('STOCKBUILD_NATIVE_DIR', '').strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(project_root / 'native' / 'runtime' / cache_tag)
    if getattr(sys, 'frozen', False):
        candidates.append(Path(sys.executable).resolve().parent / 'native' / cache_tag)
    unique = []
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return tuple(unique)


def _extension_candidates(module_name, search_dirs):
    for directory in search_dirs:
        root = Path(directory).expanduser().resolve()
        for suffix in importlib.machinery.EXTENSION_SUFFIXES:
            candidate = root / f'{module_name}{suffix}'
            if candidate.is_file():
                yield candidate


def _load_extension_path(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise NativeUnavailable(f'無法建立 native module spec: {path}')
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        if os.name == 'nt' and hasattr(os, 'add_dll_directory'):
            with os.add_dll_directory(str(Path(path).parent)):
                spec.loader.exec_module(module)
        else:
            spec.loader.exec_module(module)
    except Exception:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    return module


def load_native(module_name='_stockbuild_native', search_dirs=None):
    import_error = None
    try:
        module = importlib.import_module(module_name)
    except (ImportError, OSError) as exc:
        import_error = exc
        directories = tuple(search_dirs) if search_dirs is not None else native_runtime_dirs()
        module = None
        load_errors = []
        for candidate in _extension_candidates(module_name, directories):
            try:
                module = _load_extension_path(module_name, candidate)
                break
            except Exception as load_exc:
                load_errors.append(f'{candidate}: {load_exc}')
        if module is None:
            searched = ', '.join(str(Path(item).resolve()) for item in directories) or '(無)'
            detail = f'；候選載入錯誤：{" | ".join(load_errors)}' if load_errors else ''
            raise NativeUnavailable(
                f'無法載入 {module_name}: {import_error}；已搜尋：{searched}{detail}') from exc
    try:
        info = validate_abi_info(dict(module.abi_info()))
        module.handshake(ABI_VERSION, KBAR_SCHEMA_VERSION)
    except NativeVersionError:
        raise
    except Exception as exc:
        raise NativeVersionError(f'native 版本握手失敗: {exc}') from exc
    info = dict(info)
    info['module_file'] = str(Path(module.__file__).resolve())
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


def _batch_from_native_payload(payload):
    try:
        batch = KBarBatch(
            payload['timestamps'], payload['open'], payload['high'], payload['low'],
            payload['close'], payload['volume'], payload['flags'])
        validate_batch(batch)
        if int(payload['rows']) != batch.rows:
            raise KBarSchemaError('native rows metadata 與 KBar buffer 列數不一致')
    except KeyError as exc:
        raise KBarSchemaError(f'native KBar 結果缺少欄位: {exc}') from exc
    for column in batch.as_args():
        column.setflags(write=False)
    return batch


def resample_kbars(module, batch, mode, interval_minutes=0,
                   timezone_offset_minutes=0, session_basis='all'):
    """呼叫 ADR-147 C++ OHLCV resampler；目前只供 shadow/differential。"""
    validate_abi_info(dict(module.abi_info()))
    validate_batch(batch)
    normalized_mode = str(mode).strip().lower()
    allowed_modes = {
        'fixed', 'day', 'week', 'month',
        'future_day', 'future_week', 'future_month',
    }
    if normalized_mode not in allowed_modes:
        raise KBarSchemaError(f'不支援的 resample mode: {mode!r}')
    try:
        interval = int(interval_minutes)
        offset = int(timezone_offset_minutes)
    except (TypeError, ValueError) as exc:
        raise KBarSchemaError(f'resample interval/offset 必須是整數: {exc}') from exc
    if normalized_mode == 'fixed' and interval <= 0:
        raise KBarSchemaError('fixed resample 的 interval_minutes 必須大於 0')
    if not -1440 <= offset <= 1440:
        raise KBarSchemaError('timezone_offset_minutes 必須介於 -1440 與 1440')
    basis = str(session_basis).strip().lower()
    if normalized_mode.startswith('future_') and basis not in ('all', 'day'):
        raise KBarSchemaError('期貨 session_basis 只接受 all 或 day')
    payload = dict(module.resample_kbars(
        *batch.as_args(), normalized_mode, interval, offset, basis))
    return _batch_from_native_payload(payload)


def _positive_period(value, name, minimum=1):
    try:
        period = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise KBarSchemaError(f'{name} 必須是整數: {exc}') from exc
    if period < minimum:
        raise KBarSchemaError(f'{name} 必須大於等於 {minimum}')
    return period


def calculate_native_indicators(
        module, batch, ma_period=20, rsi_period=14,
        macd_fast=12, macd_slow=26, macd_signal=9,
        bb_period=20, bb_std_up=2.0, bb_std_down=2.0, bb_ma_type='SMA'):
    """計算 ADR-147 第一批 native 指標；輸出為共享 owner 的唯讀 arrays。"""
    validate_abi_info(dict(module.abi_info()))
    validate_batch(batch)
    periods = (
        _positive_period(ma_period, 'ma_period'),
        _positive_period(rsi_period, 'rsi_period'),
        _positive_period(macd_fast, 'macd_fast'),
        _positive_period(macd_slow, 'macd_slow'),
        _positive_period(macd_signal, 'macd_signal'),
        _positive_period(bb_period, 'bb_period', minimum=2),
    )
    try:
        sigma_up, sigma_down = float(bb_std_up), float(bb_std_down)
    except (TypeError, ValueError) as exc:
        raise KBarSchemaError(f'Bollinger sigma 必須是數值: {exc}') from exc
    if (not np.isfinite(sigma_up) or sigma_up <= 0 or
            not np.isfinite(sigma_down) or sigma_down <= 0):
        raise KBarSchemaError('Bollinger 上下 sigma 必須是 finite 正數')
    ma_type = str(bb_ma_type).strip().upper()
    if ma_type not in ('SMA', 'EMA', 'WMA'):
        raise KBarSchemaError('bb_ma_type 只接受 SMA、EMA 或 WMA')
    payload = dict(module.indicator_core(
        batch.close, *periods, sigma_up, sigma_down, ma_type))
    names = (
        'sma', 'ema', 'wma', 'rsi', 'macd', 'signal', 'hist',
        'bb_mid', 'bb_std', 'bb_upper', 'bb_lower', 'bb_width',
    )
    try:
        columns = [payload[name] for name in names]
        rows = int(payload['rows'])
    except KeyError as exc:
        raise KBarSchemaError(f'native indicator 結果缺少欄位: {exc}') from exc
    for name, column in zip(names, columns):
        if (not isinstance(column, np.ndarray) or column.ndim != 1 or
                column.dtype != np.dtype(np.float64) or not column.flags.c_contiguous):
            raise KBarSchemaError(f'native indicator {name} 不符合 float64 contiguous ABI')
        if column.size != rows or column.size != batch.rows:
            raise KBarSchemaError(f'native indicator {name} 列數不一致')
        column.setflags(write=False)
    return IndicatorBatch(*columns)


def calculate_native_moving_averages(module, batch, periods, kinds):
    """【ADR-151】一次計算多組不同週期／類型 MA，避免重跑完整 indicator core。"""
    validate_abi_info(dict(module.abi_info()))
    validate_batch(batch)
    parsed_periods = [_positive_period(value, 'ma_period') for value in periods]
    parsed_kinds = [str(value).strip().upper() for value in kinds]
    if len(parsed_periods) != len(parsed_kinds):
        raise KBarSchemaError('MA periods 與 kinds 數量不一致')
    if not parsed_periods or len(parsed_periods) > 64:
        raise KBarSchemaError('MA 請求數量必須介於 1 與 64')
    invalid = [kind for kind in parsed_kinds if kind not in ('SMA', 'EMA', 'WMA')]
    if invalid:
        raise KBarSchemaError(f'MA 類型只接受 SMA、EMA、WMA：{invalid[0]}')
    payload = dict(module.multi_ma_core(batch.close, parsed_periods, parsed_kinds))
    try:
        values = tuple(payload['values'])
        rows = int(payload['rows'])
        count = int(payload['count'])
    except (KeyError, TypeError, ValueError) as exc:
        raise KBarSchemaError(f'native multi MA 回傳缺少欄位：{exc}') from exc
    if count != len(parsed_periods) or len(values) != count:
        raise KBarSchemaError(
            f'native multi MA 欄數不一致：expected={len(parsed_periods)}, actual={count}')
    for index, column in enumerate(values):
        if (not isinstance(column, np.ndarray) or column.ndim != 1 or
                column.dtype != np.dtype(np.float64) or not column.flags.c_contiguous):
            raise KBarSchemaError(f'native multi MA 第 {index + 1} 欄不是 float64 contiguous ABI')
        if column.size != rows or column.size != batch.rows:
            raise KBarSchemaError(f'native multi MA 第 {index + 1} 欄列數不一致')
        column.setflags(write=False)
    return MultiMovingAverageBatch(values)


_NATIVE_CONDITION_SPECS = {
    'ma_cross_up': (('fast', 5), ('slow', 20)),
    'ma_cross_down': (('fast', 5), ('slow', 20)),
    'price_break_high': (('n', 20),), 'price_break_low': (('n', 20),),
    'price_above': (('value', 0),), 'price_below': (('value', 0),),
    'price_cross_up_ma': (('n', 20),), 'price_cross_down_ma': (('n', 20),),
    'price_above_ma': (('n', 20),), 'price_below_ma': (('n', 20),),
    'ma_slope_up': (('n', 20), ('look', 3)),
    'ma_slope_down': (('n', 20), ('look', 3)),
    'ma_align_bull': (('short', 5), ('mid', 20), ('long', 60)),
    'ma_align_bear': (('short', 5), ('mid', 20), ('long', 60)),
    'volume_above_ma': (('n', 20), ('mult', 1.5)),
    'volume_below_ma': (('n', 20), ('mult', 0.7)),
    'consecutive_up': (('n', 3),), 'consecutive_down': (('n', 3),),
    'pct_change_above': (('value', 3.0),),
    'pct_change_below': (('value', -3.0),),
    'always_true': (), 'inside_bar': (),
    'gap_up': (('value', 0.0),), 'gap_down': (('value', 0.0),),
}
_NATIVE_MA_CONDITIONS = frozenset({
    'price_cross_up_ma', 'price_cross_down_ma', 'price_above_ma', 'price_below_ma',
    'ma_slope_up', 'ma_slope_down', 'ma_align_bull', 'ma_align_bear',
})


def calculate_native_conditions(module, batch, conditions):
    """【ADR-152】將第一批策略條件編譯成 typed batch，回傳逐根 readonly bool bytes。"""
    validate_abi_info(dict(module.abi_info()))
    validate_batch(batch)
    requests = list(conditions or ())
    if not requests or len(requests) > 64:
        raise KBarSchemaError('native condition 請求數量必須介於 1 與 64')
    types, params, kinds = [], [], []
    for index, condition in enumerate(requests):
        if not isinstance(condition, dict):
            raise KBarSchemaError(f'native condition 第 {index + 1} 項必須是 dict')
        condition_type = str(condition.get('type', '')).strip()
        spec = _NATIVE_CONDITION_SPECS.get(condition_type)
        if spec is None:
            raise KBarSchemaError(f'尚未支援的 native condition：{condition_type or "(空)"}')
        raw_params = condition.get('params') or {}
        if not isinstance(raw_params, dict):
            raise KBarSchemaError(f'native condition {condition_type} params 必須是 dict')
        parsed = []
        for key, default in spec:
            try:
                value = float(raw_params.get(key, default))
            except (TypeError, ValueError) as exc:
                raise KBarSchemaError(
                    f'native condition {condition_type}.{key} 必須是數值：{exc}') from exc
            if not np.isfinite(value):
                raise KBarSchemaError(f'native condition {condition_type}.{key} 必須是有限數值')
            parsed.append(value)
        kind = str(raw_params.get('kind', 'SMA')).strip().upper()
        if condition_type in _NATIVE_MA_CONDITIONS and kind not in ('SMA', 'EMA'):
            raise KBarSchemaError(f'native condition {condition_type} 均線只接受 SMA 或 EMA')
        types.append(condition_type)
        params.append(parsed)
        kinds.append(kind)
    payload = dict(module.condition_core(
        batch.open, batch.high, batch.low, batch.close, batch.volume,
        types, params, kinds))
    try:
        values = tuple(payload['values'])
        rows, count = int(payload['rows']), int(payload['count'])
    except (KeyError, TypeError, ValueError) as exc:
        raise KBarSchemaError(f'native condition 回傳缺少欄位：{exc}') from exc
    if count != len(requests) or len(values) != count:
        raise KBarSchemaError('native condition 回傳欄數不一致')
    for index, column in enumerate(values):
        if (not isinstance(column, np.ndarray) or column.ndim != 1 or
                column.dtype != np.dtype(np.uint8) or not column.flags.c_contiguous):
            raise KBarSchemaError(f'native condition 第 {index + 1} 欄不是 uint8 contiguous ABI')
        if column.size != rows or column.size != batch.rows:
            raise KBarSchemaError(f'native condition 第 {index + 1} 欄列數不一致')
        column.setflags(write=False)
    return ConditionBatch(values)


def calculate_native_advanced_indicators(
        module, batch, kdj_n=9, kdj_m1=3, kdj_m2=3, dmi_n=14,
        jae_a_period=14, jae_j_n=9, jae_j_m1=3, jae_j_m2=3,
        jae_e_period=60):
    """計算 ADR-148 KDJ／DMI／JAE；目前只供 shadow/differential。"""
    validate_abi_info(dict(module.abi_info()))
    validate_batch(batch)
    values = (
        _positive_period(kdj_n, 'kdj_n'),
        _positive_period(kdj_m1, 'kdj_m1'),
        _positive_period(kdj_m2, 'kdj_m2'),
        _positive_period(dmi_n, 'dmi_n'),
        _positive_period(jae_a_period, 'jae_a_period'),
        _positive_period(jae_j_n, 'jae_j_n'),
        _positive_period(jae_j_m1, 'jae_j_m1'),
        _positive_period(jae_j_m2, 'jae_j_m2'),
        _positive_period(jae_e_period, 'jae_e_period'),
    )
    payload = dict(module.advanced_indicator_core(
        batch.high, batch.low, batch.close, *values))
    names = (
        'rsv', 'k', 'd', 'j', 'plus_dm', 'minus_dm',
        'plus_di', 'minus_di', 'adx', 'jae_a', 'jae_j', 'jae_e',
    )
    try:
        columns = [payload[name] for name in names]
        rows = int(payload['rows'])
    except KeyError as exc:
        raise KBarSchemaError(f'native advanced indicator 結果缺少欄位: {exc}') from exc
    for name, column in zip(names, columns):
        if (not isinstance(column, np.ndarray) or column.ndim != 1 or
                column.dtype != np.dtype(np.float64) or not column.flags.c_contiguous):
            raise KBarSchemaError(
                f'native advanced indicator {name} 不符合 float64 contiguous ABI')
        if column.size != rows or column.size != batch.rows:
            raise KBarSchemaError(f'native advanced indicator {name} 列數不一致')
        column.setflags(write=False)
    return AdvancedIndicatorBatch(*columns)
