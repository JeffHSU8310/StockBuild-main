# -*- coding: utf-8 -*-
"""ADR-154：正式 Python 回測與 native intent stream 的唯讀 shadow A/B。"""
from __future__ import annotations

import copy

import numpy as np

from . import backtest, native_bridge


class NativeBacktestMismatch(RuntimeError):
    """Raised only when a caller explicitly requests strict shadow parity."""


_TIME_WINDOW_KEYS = (
    'entry_time_start', 'entry_time_end', 'exit_time_start', 'exit_time_end',
    'specific_entry_time',
)


def eligibility(strategy):
    """Return (supported, reason) without silently weakening strategy semantics."""
    if not isinstance(strategy, dict):
        return False, 'strategy must be a dict'
    if strategy.get('kind') == 'custom' or strategy.get('source_code'):
        return False, 'custom Python strategy is not supported by ADR-154 shadow'
    if strategy.get('buy_and_hold'):
        return False, 'buy-and-hold accumulation/DCA is not supported by ADR-154 shadow'
    if any(strategy.get(key) for key in _TIME_WINDOW_KEYS):
        return False, 'entry/exit time windows are not supported by ADR-154 shadow'
    if strategy.get('intrabar_stop') or strategy.get('intrabar_stop_enabled'):
        return False, 'intrabar stop requires the later native fill engine stage'
    entries = list(strategy.get('entry') or ())
    exits = list(strategy.get('exit_signals') or ())
    if not entries:
        return False, 'strategy has no entry conditions'
    supported = native_bridge._NATIVE_CONDITION_SPECS
    unsupported = [str(item.get('type', '')) for item in entries + exits
                   if not isinstance(item, dict) or item.get('type') not in supported]
    if unsupported:
        return False, 'unsupported native conditions: ' + ', '.join(unsupported)
    return True, ''


def _native_trace(module, strategy, df):
    batch = native_bridge.prepare_kbars(df)
    rows = batch.rows
    execution_prices = np.ascontiguousarray(batch.open.copy())
    if rows:
        execution_prices[:-1] = batch.open[1:]
        execution_prices[-1] = batch.close[-1]
    result = native_bridge.evaluate_native_strategy(
        module, batch, list(strategy.get('entry') or ()),
        list(strategy.get('exit_signals') or ()),
        direction='SHORT' if strategy.get('direction') == '做空' else 'LONG',
        quantity=int(strategy.get('qty', 1) or 1),
        entry_logic='AND', exit_logic='OR',
        stop_loss_pct=float(strategy.get('stop_loss_pct', 0) or 0),
        take_profit_pct=float(strategy.get('take_profit_pct', 0) or 0),
        stop_loss_abs=float(strategy.get('stop_loss_abs', 0) or 0),
        take_profit_abs=float(strategy.get('take_profit_abs', 0) or 0),
        execution_prices=execution_prices,
        decision_start_row=2,
        decision_end_row=max(2, rows - 1),
    )
    trace = []
    for decision_row in np.flatnonzero(result.kind):
        execution_row = int(decision_row) + 1
        trace.append({
            'decision_row': int(decision_row),
            'execution_row': execution_row,
            'decision_ts': df.index[int(decision_row)],
            'execution_ts': df.index[execution_row],
            'kind': 'OPEN' if int(result.kind[decision_row]) == 1 else 'CLOSE',
            'action': '買進' if int(result.action[decision_row]) == 1 else '賣出',
            'quantity': int(result.quantity[decision_row]),
            'price': float(result.price[decision_row]),
            'reason_code': int(result.reason[decision_row]),
        })
    return trace


def _event_projection(event):
    return {
        key: event[key] for key in (
            'decision_row', 'execution_row', 'kind', 'action', 'quantity', 'price')
    }


def _compare(python_trace, native_trace):
    mismatches = []
    count = max(len(python_trace), len(native_trace))
    for index in range(count):
        py_event = _event_projection(python_trace[index]) if index < len(python_trace) else None
        native_event = _event_projection(native_trace[index]) if index < len(native_trace) else None
        if py_event is None or native_event is None:
            mismatches.append({'event': index, 'python': py_event, 'native': native_event})
            continue
        unequal = {
            key: {'python': py_event[key], 'native': native_event[key]}
            for key in py_event
            if (not np.isclose(py_event[key], native_event[key], rtol=0.0, atol=1e-12)
                if key == 'price' else py_event[key] != native_event[key])
        }
        if unequal:
            mismatches.append({'event': index, 'differences': unequal})
    return mismatches


def run_shadow(module, strategy, df, *, strict=False, **backtest_kwargs):
    """Run authoritative Python backtest and compare native T+1 intents.

    The returned ``result`` is always the unmodified Python authority. Native output
    cannot place orders or replace accounting in this stage.
    """
    python_trace = []
    result = backtest.run_backtest(
        strategy, df, _intent_trace=python_trace, **backtest_kwargs)
    supported, reason = eligibility(strategy)
    if not supported:
        return {
            'status': 'not_applicable', 'reason': reason, 'result': result,
            'python_intents': len(python_trace), 'native_intents': 0,
            'mismatches': [],
        }
    if df is None or len(df) < 3:
        native_trace = []
    else:
        shadow_strategy = backtest._relax_realtime_guards(copy.deepcopy(strategy))
        native_trace = _native_trace(module, shadow_strategy, df)
    mismatches = _compare(python_trace, native_trace)
    report = {
        'status': 'passed' if not mismatches else 'failed',
        'reason': '', 'result': result,
        'python_intents': len(python_trace),
        'native_intents': len(native_trace),
        'mismatch_count': len(mismatches),
        'mismatches': mismatches[:50],
    }
    if strict and mismatches:
        raise NativeBacktestMismatch(
            f'native backtest shadow differs at {len(mismatches)} intent event(s)')
    return report
