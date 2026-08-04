"""Native engine routing contracts for shared indicator consumers.

ADR149 deliberately enables only ``off`` and ``shadow``.  ``native`` is a
reserved mode whose activation requires real-data parity and rollout evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

from core import native_bridge


MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_NATIVE = "native"
SUPPORTED_MODES = (MODE_OFF, MODE_SHADOW)


class IndicatorRouteError(RuntimeError):
    """Raised when an explicitly selected indicator route cannot run safely."""


class IndicatorParityError(IndicatorRouteError):
    """Raised when native shadow output differs from the Python authority."""


@dataclass(frozen=True)
class NativeIndicatorOutput:
    columns: Mapping[str, np.ndarray]
    native_version: str
    elapsed_ms: float


@dataclass(frozen=True)
class IndicatorRouteResult:
    frame: pd.DataFrame
    telemetry: Mapping[str, Any]


NativeProvider = Callable[[pd.DataFrame, Mapping[str, Any]], NativeIndicatorOutput]


def normalize_indicator_mode(value: Any) -> str:
    """Normalize the persisted feature flag and reject unapproved activation."""

    mode = str(value or MODE_OFF).strip().lower()
    if mode in SUPPORTED_MODES:
        return mode
    if mode == MODE_NATIVE:
        raise IndicatorRouteError(
            "native 指標模式尚未開放；必須先通過 ADR149 shadow 真實資料驗收"
        )
    raise IndicatorRouteError(
        f"未知的 native 指標模式：{value!r}（目前僅支援 off、shadow）"
    )


def route_indicators(
    source: pd.DataFrame,
    python_result: pd.DataFrame,
    settings: Mapping[str, Any],
    *,
    mode: Any = MODE_OFF,
    native_provider: NativeProvider | None = None,
) -> IndicatorRouteResult:
    """Return Python output and, in shadow mode, require native parity first."""

    normalized_mode = normalize_indicator_mode(mode)
    if normalized_mode == MODE_OFF:
        return IndicatorRouteResult(
            frame=python_result,
            telemetry={"mode": MODE_OFF, "status": "disabled", "compared_columns": 0},
        )

    if not _has_supported_columns(settings):
        return IndicatorRouteResult(
            frame=python_result,
            telemetry={
                "mode": MODE_SHADOW,
                "status": "no_enabled_columns",
                "compared_columns": 0,
            },
        )

    provider = native_provider or calculate_native_columns
    try:
        native_output = provider(source, settings)
    except IndicatorRouteError:
        raise
    except Exception as exc:
        raise IndicatorRouteError(f"native 指標 shadow 無法執行：{exc}") from exc

    if not native_output.columns:
        return IndicatorRouteResult(
            frame=python_result,
            telemetry={
                "mode": MODE_SHADOW,
                "status": "no_enabled_columns",
                "compared_columns": 0,
                "native_ms": native_output.elapsed_ms,
                "native_version": native_output.native_version,
            },
        )

    max_abs_error = 0.0
    for column, actual_raw in native_output.columns.items():
        if column not in python_result.columns:
            raise IndicatorParityError(f"Python 指標結果缺少 native 欄位：{column}")

        expected = python_result[column].to_numpy(dtype=np.float64, copy=False)
        actual = np.asarray(actual_raw, dtype=np.float64)
        if expected.shape != actual.shape:
            raise IndicatorParityError(
                f"{column} 長度不一致：Python={expected.size}、native={actual.size}"
            )

        expected_nan = np.isnan(expected)
        actual_nan = np.isnan(actual)
        if not np.array_equal(expected_nan, actual_nan):
            first = int(np.flatnonzero(expected_nan != actual_nan)[0])
            raise IndicatorParityError(f"{column} NaN 位置不一致（索引 {first}）")

        finite = ~expected_nan
        if finite.any():
            differences = np.abs(expected[finite] - actual[finite])
            column_max = float(differences.max(initial=0.0))
            max_abs_error = max(max_abs_error, column_max)
            rtol, atol = _column_tolerance(column)
            matches = np.isclose(expected[finite], actual[finite], rtol=rtol, atol=atol)
            if not bool(matches.all()):
                finite_positions = np.flatnonzero(finite)
                first = int(finite_positions[np.flatnonzero(~matches)[0]])
                raise IndicatorParityError(
                    f"{column} 數值不一致（索引 {first}，最大絕對誤差 {column_max:.3e}）"
                )

    return IndicatorRouteResult(
        frame=python_result,
        telemetry={
            "mode": MODE_SHADOW,
            "status": "passed",
            "compared_columns": len(native_output.columns),
            "max_abs_error": max_abs_error,
            "native_ms": native_output.elapsed_ms,
            "native_version": native_output.native_version,
        },
    )


def calculate_native_columns(
    source: pd.DataFrame,
    settings: Mapping[str, Any],
    *,
    module: Any | None = None,
) -> NativeIndicatorOutput:
    """Calculate the ADR147/148 columns enabled by the shared route settings."""

    started = perf_counter()
    native_module, native_version = _resolve_native_module(module)
    batch = _prepare_indicator_batch(source)
    columns: dict[str, np.ndarray] = {}

    bb_enabled = bool(settings.get("bb_enabled", False))
    macd_enabled = bool(settings.get("macd_enabled", False))
    rsi_enabled = bool(settings.get("rsi_enabled", False))
    if bb_enabled or macd_enabled or rsi_enabled:
        first = native_bridge.calculate_native_indicators(
            native_module,
            batch,
            ma_period=_positive_int(settings.get("bb_period", 20), "bb_period"),
            rsi_period=_positive_int(settings.get("rsi_period", 14), "rsi_period"),
            macd_fast=_positive_int(settings.get("macd_fast", 12), "macd_fast"),
            macd_slow=_positive_int(settings.get("macd_slow", 26), "macd_slow"),
            macd_signal=_positive_int(settings.get("macd_signal", 9), "macd_signal"),
            bb_period=_positive_int(settings.get("bb_period", 20), "bb_period"),
            bb_std_up=_positive_float(settings.get("bb_std_up", 2.0), "bb_std_up"),
            bb_std_down=_positive_float(settings.get("bb_std_down", 2.0), "bb_std_down"),
            bb_ma_type=str(settings.get("bb_ma_type", "SMA")).upper(),
        )
        if bb_enabled:
            _copy_attributes(first, columns, {
                "BB_MID": "bb_mid", "BB_STD": "bb_std", "BB_UPPER": "bb_upper",
                "BB_LOWER": "bb_lower", "BB_WIDTH": "bb_width",
            })
        if macd_enabled:
            _copy_attributes(first, columns, {
                "MACD": "macd", "Signal": "signal", "Hist": "hist",
            })
        if rsi_enabled:
            _copy_attributes(first, columns, {"RSI": "rsi"})

    if bool(settings.get("bb2_enabled", False)):
        second = native_bridge.calculate_native_indicators(
            native_module,
            batch,
            ma_period=_positive_int(settings.get("bb2_period", 20), "bb2_period"),
            rsi_period=14,
            macd_fast=12,
            macd_slow=26,
            macd_signal=9,
            bb_period=_positive_int(settings.get("bb2_period", 20), "bb2_period"),
            bb_std_up=_positive_float(settings.get("bb2_std_up", 2.0), "bb2_std_up"),
            bb_std_down=_positive_float(settings.get("bb2_std_down", 2.0), "bb2_std_down"),
            bb_ma_type=str(settings.get("bb2_ma_type", "SMA")).upper(),
        )
        columns.update(
            {
                "BB2_MID": second.bb_mid,
                "BB2_STD": second.bb_std,
                "BB2_UPPER": second.bb_upper,
                "BB2_LOWER": second.bb_lower,
            }
        )

    kdj_enabled = bool(settings.get("kdj_enabled", False))
    dmi_enabled = bool(settings.get("dmi_enabled", False))
    jae_enabled = bool(settings.get("jae_enabled", False))
    if kdj_enabled or dmi_enabled or jae_enabled:
        jae_params = dict(settings.get("jae_params") or {})
        advanced = native_bridge.calculate_native_advanced_indicators(
            native_module,
            batch,
            kdj_n=_positive_int(settings.get("kdj_period", 9), "kdj_period"),
            kdj_m1=_positive_int(settings.get("k_smooth", 3), "k_smooth"),
            kdj_m2=_positive_int(settings.get("d_smooth", 3), "d_smooth"),
            dmi_n=_positive_int(settings.get("dmi_period", 14), "dmi_period"),
            jae_a_period=_positive_int(jae_params.get("a_period", 14), "jae_a_period"),
            jae_j_n=_positive_int(jae_params.get("j_n", 9), "jae_j_n"),
            jae_j_m1=_positive_int(jae_params.get("j_m1", 3), "jae_j_m1"),
            jae_j_m2=_positive_int(jae_params.get("j_m2", 3), "jae_j_m2"),
            jae_e_period=_positive_int(jae_params.get("e_period", 60), "jae_e_period"),
        )
        if kdj_enabled:
            _copy_attributes(advanced, columns, {"RSV": "rsv", "K": "k", "D": "d", "J": "j"})
        if dmi_enabled:
            _copy_attributes(advanced, columns, {
                "+DM": "plus_dm", "-DM": "minus_dm", "+DI": "plus_di",
                "-DI": "minus_di", "ADX": "adx",
            })
        if jae_enabled:
            _copy_attributes(advanced, columns, {
                "JAE_A": "jae_a", "JAE_J": "jae_j", "JAE_E": "jae_e",
            })

    elapsed_ms = (perf_counter() - started) * 1000.0
    return NativeIndicatorOutput(columns, native_version, elapsed_ms)


def probe_native() -> Mapping[str, str]:
    """驗證產品 runtime 可載入及 ABI 握手，供 GUI 明確啟用 shadow 前檢查。"""
    module, version = _load_native_module()
    return {
        "native_version": version,
        "module_file": str(Path(module.__file__).resolve()),
    }


@lru_cache(maxsize=1)
def _load_native_module() -> tuple[Any, str]:
    try:
        module, info = native_bridge.load_native()
    except Exception as exc:
        raise IndicatorRouteError(f"無法載入 _stockbuild_native：{exc}") from exc
    return module, str(info.get("native_version", "unknown"))


def _resolve_native_module(module: Any | None) -> tuple[Any, str]:
    if module is None:
        return _load_native_module()
    try:
        info = native_bridge.validate_abi_info(dict(module.abi_info()))
        module.handshake(native_bridge.ABI_VERSION, native_bridge.KBAR_SCHEMA_VERSION)
    except Exception as exc:
        raise IndicatorRouteError(f"native ABI 驗證失敗：{exc}") from exc
    return module, str(info.get("native_version", "unknown"))


def _prepare_indicator_batch(source: pd.DataFrame) -> native_bridge.KBarBatch:
    required = {"High", "Low", "Close"}
    missing = sorted(required.difference(source.columns))
    if missing:
        raise IndicatorRouteError(f"指標來源缺少欄位：{', '.join(missing)}")
    prepared = source.copy()
    if "Open" not in prepared.columns:
        prepared["Open"] = prepared["Close"]
    if "Volume" not in prepared.columns:
        prepared["Volume"] = 0.0
    return native_bridge.prepare_kbars(prepared)


def _copy_attributes(
    source: Any,
    destination: dict[str, np.ndarray],
    names: Mapping[str, str],
) -> None:
    for destination_name, source_name in names.items():
        destination[destination_name] = np.asarray(getattr(source, source_name), dtype=np.float64)


def _positive_int(value: Any, name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise IndicatorRouteError(f"{name} 必須是正整數")
    return parsed


def _positive_float(value: Any, name: str) -> float:
    parsed = float(value)
    if not np.isfinite(parsed) or parsed <= 0.0:
        raise IndicatorRouteError(f"{name} 必須是有限正數")
    return parsed


def _column_tolerance(column: str) -> tuple[float, float]:
    if column.startswith("BB"):
        return 1e-9, 1e-8
    return 2e-10, 2e-10


def _has_supported_columns(settings: Mapping[str, Any]) -> bool:
    return any(bool(settings.get(name, False)) for name in (
        "bb_enabled", "bb2_enabled", "macd_enabled", "rsi_enabled",
        "kdj_enabled", "dmi_enabled", "jae_enabled",
    ))
