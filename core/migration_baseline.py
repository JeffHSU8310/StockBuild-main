# -*- coding: utf-8 -*-
"""ADR-144：C++／Qt 移植前的可重現黃金輸出與效能紀錄工具。

本模組不改變任何產品路徑，只把 Python 參考引擎的輸出轉成穩定、可雜湊、
可逐欄比較的 JSON 結構。未來 native shadow mode 必須用同一份規則比較，
不能只比一個總損益或硬編「看起來差不多」的績效值。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform
import sys
from collections.abc import Mapping


SCHEMA_VERSION = 1
FLOAT_DIGITS = 8


class GoldenMismatch(AssertionError):
    """黃金輸出與目前參考引擎不一致。"""


def _scalar(value):
    """把 NumPy／pandas scalar 安全降成 Python scalar，不強制依賴兩者。"""
    item = getattr(value, "item", None)
    if callable(item) and not isinstance(value, (str, bytes, bytearray)):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    return value


def canonicalize(value, *, float_digits=FLOAT_DIGITS):
    """轉成跨機可重現的 JSON-safe 結構。

    dict key 固定排序、timestamp 固定 ISO-8601、浮點固定有效小數位；NaN／Inf
    用明確字串保存，避免不同 JSON encoder 產生非標準值。
    """
    value = _scalar(value)
    if isinstance(value, Mapping):
        return {str(k): canonicalize(value[k], float_digits=float_digits)
                for k in sorted(value, key=lambda x: str(x))}
    if isinstance(value, (list, tuple)):
        return [canonicalize(v, float_digits=float_digits) for v in value]
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    # pandas.Timestamp 不需要 import pandas；它也提供 isoformat()。
    if value.__class__.__name__ in ("Timestamp", "Timedelta"):
        iso = getattr(value, "isoformat", None)
        return iso() if callable(iso) else str(value)
    if isinstance(value, float):
        if value != value:
            return "NaN"
        if value == float("inf"):
            return "Infinity"
        if value == float("-inf"):
            return "-Infinity"
        rounded = round(value, int(float_digits))
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def stable_json(value):
    """回傳供雜湊與 fixture 檢閱使用的唯一 JSON 表示。"""
    return json.dumps(canonicalize(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def stable_hash(value):
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def make_golden_bundle(components, fixture):
    """建立版本化黃金 bundle，並為每個功能輸出獨立雜湊。"""
    clean_components = canonicalize(components)
    body = {
        "schema_version": SCHEMA_VERSION,
        "fixture": canonicalize(fixture),
        "components": clean_components,
        "component_hashes": {
            name: stable_hash(value) for name, value in clean_components.items()
        },
    }
    body["bundle_hash"] = stable_hash(body)
    return body


def compare_golden(expected, actual, *, max_differences=50):
    """逐欄比較，回傳 JSONPath 風格差異；空 list 表示完全一致。"""
    left = canonicalize(expected)
    right = canonicalize(actual)
    differences = []

    def walk(a, b, path):
        if len(differences) >= max_differences:
            return
        if type(a) is not type(b):
            differences.append(f"{path}: type {type(a).__name__} != {type(b).__name__}")
            return
        if isinstance(a, dict):
            for key in sorted(set(a) | set(b)):
                child = f"{path}.{key}"
                if key not in a:
                    differences.append(f"{child}: unexpected")
                elif key not in b:
                    differences.append(f"{child}: missing")
                else:
                    walk(a[key], b[key], child)
                if len(differences) >= max_differences:
                    return
            return
        if isinstance(a, list):
            if len(a) != len(b):
                differences.append(f"{path}: length {len(a)} != {len(b)}")
            for i, (av, bv) in enumerate(zip(a, b)):
                walk(av, bv, f"{path}[{i}]")
                if len(differences) >= max_differences:
                    return
            return
        if a != b:
            differences.append(f"{path}: {a!r} != {b!r}")

    walk(left, right, "$")
    return differences


def verify_golden(expected, actual):
    differences = compare_golden(expected, actual)
    if differences:
        raise GoldenMismatch("黃金輸出不一致：\n" + "\n".join(differences))
    return True


def environment_record():
    """效能報告必備環境欄位；禁止拿不同環境的毫秒數直接互比。"""
    versions = {}
    for name in ("numpy", "pandas"):
        try:
            module = __import__(name)
            versions[name] = str(module.__version__)
        except Exception:
            versions[name] = None
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable": os.path.basename(sys.executable),
        "packages": versions,
    }
