# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""CEL expression evaluator for the Termin runtime.

Uses Google's Common Expression Language (CEL) via cel-python.
CEL is non-Turing-complete, formally specified, and has matching
implementations in Python, JavaScript, Rust, and Go.

Expressions use standard function-call syntax:
  sum(items), upper(name), clamp(n, 0, 100), daysBetween(a, b)

Built-in CEL functions (no registration needed):
  size(), contains(), startsWith(), endsWith(), has()

System context variables (injected fresh per evaluation):
  now, today
"""

import math
from datetime import datetime, date, timedelta

import celpy
from celpy import json_to_cel
from celpy.celtypes import StringType, DoubleType, IntType, BoolType, ListType


# ── System Functions ──

def _cel_sum(items):
    return DoubleType(sum(float(x) for x in items))

def _cel_avg(items):
    vals = [float(x) for x in items]
    return DoubleType(sum(vals) / len(vals)) if vals else DoubleType(0)

def _cel_min(items):
    vals = [float(x) for x in items]
    return DoubleType(min(vals)) if vals else IntType(0)

def _cel_max(items):
    vals = [float(x) for x in items]
    return DoubleType(max(vals)) if vals else IntType(0)

def _cel_flatten(items):
    result = []
    for sub in items:
        if hasattr(sub, '__iter__') and not isinstance(sub, (str, StringType)):
            result.extend(sub)
        else:
            result.append(sub)
    return ListType(result)

def _cel_unique(items):
    seen = []
    for x in items:
        if x not in seen:
            seen.append(x)
    return ListType(seen)

def _cel_first(items):
    return items[IntType(0)] if items else BoolType(False)

def _cel_last(items):
    return items[IntType(len(items) - 1)] if items else BoolType(False)

def _cel_sort(items):
    return ListType(sorted(items))

def _cel_days_between(d1, d2):
    try:
        a = date.fromisoformat(str(d1)[:10])
        b = date.fromisoformat(str(d2)[:10])
        return IntType(abs((b - a).days))
    except (ValueError, TypeError):
        return IntType(0)

def _cel_days_until(d):
    try:
        target = date.fromisoformat(str(d)[:10])
        return IntType((target - date.today()).days)
    except (ValueError, TypeError):
        return IntType(0)

def _cel_add_days(d, n):
    try:
        base = date.fromisoformat(str(d)[:10])
        return StringType((base + timedelta(days=int(n))).isoformat())
    except (ValueError, TypeError):
        return d

def _cel_upper(s):
    return StringType(str(s).upper()) if s else StringType("")

def _cel_lower(s):
    return StringType(str(s).lower()) if s else StringType("")

def _cel_trim(s):
    return StringType(str(s).strip()) if s else StringType("")

def _cel_replace(s, old, new):
    return StringType(str(s).replace(str(old), str(new))) if s else StringType("")

def _cel_round(n, d=IntType(0)):
    return DoubleType(round(float(n), int(d))) if n is not None else DoubleType(0)

def _cel_floor(n):
    return IntType(math.floor(float(n))) if n is not None else IntType(0)

def _cel_ceil(n):
    return IntType(math.ceil(float(n))) if n is not None else IntType(0)

def _cel_abs(n):
    return DoubleType(abs(float(n))) if n is not None else DoubleType(0)

def _cel_clamp(n, lo, hi):
    return DoubleType(max(float(lo), min(float(n), float(hi))))


SYSTEM_FUNCTIONS = {
    # Aggregation
    "sum": _cel_sum,
    "avg": _cel_avg,
    "min": _cel_min,
    "max": _cel_max,
    # Collection
    "flatten": _cel_flatten,
    "unique": _cel_unique,
    "first": _cel_first,
    "last": _cel_last,
    "sort": _cel_sort,
    # Temporal
    "daysBetween": _cel_days_between,
    "daysUntil": _cel_days_until,
    "addDays": _cel_add_days,
    # String
    "upper": _cel_upper,
    "lower": _cel_lower,
    "trim": _cel_trim,
    "replace": _cel_replace,
    # Math
    "round": _cel_round,
    "floor": _cel_floor,
    "ceil": _cel_ceil,
    "abs": _cel_abs,
    "clamp": _cel_clamp,
}


def _make_dynamic_context():
    """Build context variables that are evaluated fresh each call."""
    return {
        "now": datetime.utcnow().isoformat() + "Z",
        "today": date.today().isoformat(),
    }


class ExpressionEvaluator:
    """CEL expression evaluator with system functions and dynamic context."""

    def __init__(self):
        self._env = celpy.Environment()
        self._custom_functions = dict(SYSTEM_FUNCTIONS)

    def register_function(self, name, fn):
        """Register a custom function available in CEL expressions."""
        self._custom_functions[name] = fn

    def evaluate(self, expression, context=None):
        """Evaluate a CEL expression string against a context dict.

        Context values can be plain Python dicts/lists/strings/numbers —
        json_to_cel() handles the conversion to CEL types.

        v0.9 Phase 6a.4: source-level `the user` is rewritten to the
        CEL identifier `the_user` (CEL doesn't allow spaces in
        identifiers). The `the_user` binding in the context provides
        the BRD #3 §4.2-shaped Principal record (id, display_name,
        is_anonymous, is_system, scopes, preferences).
        """
        expression = _rewrite_the_user(expression)
        ctx_dict = _make_dynamic_context()
        if context:
            ctx_dict.update(context)
        cel_ctx = json_to_cel(ctx_dict)
        ast = self._env.compile(expression)
        prog = self._env.program(ast, functions=self._custom_functions)
        result = prog.evaluate(cel_ctx)
        # Convert CEL types back to Python for downstream consumers
        return _cel_to_python(result)


# v0.9 Phase 6a.4: source uses `the user.X` per BRD #3 §4.2 but CEL
# doesn't allow spaces in identifiers, so we rewrite `the user` →
# `the_user` before compile. The CEL context binds `the_user` to the
# BRD-shaped Principal record. Word-boundary regex prevents stomping
# on identifiers that happen to contain "the user" as a substring
# (e.g., a content named "weather user" — vanishingly unlikely but
# the bound is cheap insurance).
import re as _re
_THE_USER_PATTERN = _re.compile(r"\bthe user\b")


def _rewrite_the_user(expression: str) -> str:
    if not isinstance(expression, str) or "the user" not in expression:
        return expression
    return _THE_USER_PATTERN.sub("the_user", expression)


def _cel_to_python(value):
    """Convert CEL result types back to plain Python values."""
    if isinstance(value, BoolType):
        return bool(value)
    if isinstance(value, IntType):
        return int(value)
    if isinstance(value, DoubleType):
        return float(value)
    if isinstance(value, StringType):
        return str(value)
    if isinstance(value, ListType):
        return [_cel_to_python(x) for x in value]
    # MapType or unknown — try to return as-is
    return value
