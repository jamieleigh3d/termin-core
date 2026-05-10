# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""CEL-condition state transitions at the runtime layer (v0.9.4 Gap #3).

The compiler-side work (test_cel_transition_v094.py in
termin-compiler) carries condition_expr through AST → IR. This file
exercises the runtime side: ``do_state_transition`` evaluates
``condition_expr`` against the record context via ``expr_eval`` and
refuses the transition when the result is falsy.

Forward-compat note: the legacy v0.9.3 sm_lookup transition value
shape was a bare scope string. The v0.9.4 shape is a dict carrying
``required_scope`` + ``condition_expr``. The runtime accepts both
shapes — the legacy string still parses as a scope-only transition.
"""

from __future__ import annotations

import pytest

from termin_core.errors import (
    TerminBadRequestError,
    TerminConflictError,
    TerminScopeError,
)
from termin_core.state import do_state_transition


class _FakeStorage:
    """Minimal storage stub — just enough for do_state_transition."""

    def __init__(self, record):
        self._record = dict(record)
        self._update_calls = []

    async def read(self, table, record_id):
        return dict(self._record)

    async def update_if(self, table, record_id, condition, patch):
        # Accept the update unconditionally (we're testing the
        # condition-check path, not the CAS race path).
        from termin_core.providers.storage_contract import UpdateResult
        self._record.update(patch)
        self._update_calls.append({"patch": dict(patch)})
        return UpdateResult(applied=True, record=dict(self._record), reason="applied")


class _FakeExprEval:
    """Minimal CEL evaluator — supports bool field access and
    primitive comparisons enough for these tests."""

    def __init__(self, return_map=None, raise_on=None):
        self._return_map = return_map or {}
        self._raise_on = raise_on
        self.calls = []

    def evaluate(self, expression, ctx):
        self.calls.append({"expression": expression, "ctx": ctx})
        if self._raise_on and self._raise_on in expression:
            raise ValueError(f"fake CEL error on {expression}")
        if expression in self._return_map:
            return self._return_map[expression]
        # Fallback: walk dotted paths against ctx.
        parts = expression.split(".")
        value = ctx
        try:
            for p in parts:
                if isinstance(value, dict):
                    value = value[p]
                else:
                    value = getattr(value, p)
            return value
        except (KeyError, AttributeError):
            return False


def _make_sm_lookup(transitions):
    """Build a sm_lookup dict for a single 'sessions.lifecycle' machine."""
    return {
        "sessions": [{
            "machine_name": "lifecycle",
            "column": "lifecycle",
            "initial": "scenario",
            "transitions": transitions,
        }],
    }


_USER = {
    "role": "anonymous",
    "scopes": ["play"],
    "the_user": {
        "id": "u1",
        "display_name": "Anonymous",
        "is_anonymous": True,
        "is_system": False,
        "scopes": ["play"],
    },
}


# ── condition_expr satisfied → transition succeeds ──

@pytest.mark.asyncio
async def test_cel_transition_allowed_when_condition_true():
    storage = _FakeStorage({"id": 1, "lifecycle": "scenario", "hatch_unlocked": True})
    sm = _make_sm_lookup({
        ("scenario", "scoring"): {
            "required_scope": "",
            "condition_expr": "session.hatch_unlocked",
        },
    })
    expr_eval = _FakeExprEval()
    result = await do_state_transition(
        storage, "sessions", 1, "lifecycle", "scoring",
        _USER, sm, expr_eval=expr_eval,
    )
    assert result["lifecycle"] == "scoring"
    assert len(expr_eval.calls) == 1
    assert expr_eval.calls[0]["expression"] == "session.hatch_unlocked"


# ── condition_expr unsatisfied → conflict raised ──

@pytest.mark.asyncio
async def test_cel_transition_refused_when_condition_false():
    storage = _FakeStorage({"id": 1, "lifecycle": "scenario", "hatch_unlocked": False})
    sm = _make_sm_lookup({
        ("scenario", "scoring"): {
            "required_scope": "",
            "condition_expr": "session.hatch_unlocked",
        },
    })
    expr_eval = _FakeExprEval()
    with pytest.raises(TerminConflictError) as excinfo:
        await do_state_transition(
            storage, "sessions", 1, "lifecycle", "scoring",
            _USER, sm, expr_eval=expr_eval,
        )
    assert "session.hatch_unlocked" in str(excinfo.value)
    # The record stayed in its source state (no update_if call).
    assert storage._update_calls == []


# ── CEL evaluator missing → fail closed ──

@pytest.mark.asyncio
async def test_cel_transition_fails_closed_when_no_evaluator():
    """A misconfigured runtime that doesn't pass expr_eval must NOT
    silently allow a CEL transition."""
    storage = _FakeStorage({"id": 1, "lifecycle": "scenario", "hatch_unlocked": True})
    sm = _make_sm_lookup({
        ("scenario", "scoring"): {
            "required_scope": "",
            "condition_expr": "session.hatch_unlocked",
        },
    })
    with pytest.raises(TerminBadRequestError) as excinfo:
        await do_state_transition(
            storage, "sessions", 1, "lifecycle", "scoring",
            _USER, sm, expr_eval=None,
        )
    assert "no expression evaluator" in str(excinfo.value)
    assert storage._update_calls == []


# ── CEL evaluator throws → bad-request, not silent allow ──

@pytest.mark.asyncio
async def test_cel_transition_evaluator_exception_is_bad_request():
    storage = _FakeStorage({"id": 1, "lifecycle": "scenario"})
    sm = _make_sm_lookup({
        ("scenario", "scoring"): {
            "required_scope": "",
            "condition_expr": "broken_expression",
        },
    })
    expr_eval = _FakeExprEval(raise_on="broken_expression")
    with pytest.raises(TerminBadRequestError) as excinfo:
        await do_state_transition(
            storage, "sessions", 1, "lifecycle", "scoring",
            _USER, sm, expr_eval=expr_eval,
        )
    assert "broken_expression" in str(excinfo.value)
    assert storage._update_calls == []


# ── Forward-compat: legacy bare-scope value still works ──

@pytest.mark.asyncio
async def test_legacy_bare_scope_string_still_works():
    """Pre-Gap-#3 sm_lookup carried scope as a bare string. The
    runtime must continue to handle that shape transparently —
    new builds emit the dict shape, but tests / external runtimes
    might still use the string."""
    storage = _FakeStorage({"id": 1, "lifecycle": "scenario"})
    # Bare string value, not the new dict shape.
    sm = _make_sm_lookup({
        ("scenario", "scoring"): "play",
    })
    result = await do_state_transition(
        storage, "sessions", 1, "lifecycle", "scoring",
        _USER, sm,
    )
    assert result["lifecycle"] == "scoring"


# ── Dict shape with scope (no CEL) — equivalent to bare-scope ──

@pytest.mark.asyncio
async def test_dict_shape_scope_only_works():
    storage = _FakeStorage({"id": 1, "lifecycle": "scenario"})
    sm = _make_sm_lookup({
        ("scenario", "scoring"): {
            "required_scope": "play",
            "condition_expr": None,
        },
    })
    result = await do_state_transition(
        storage, "sessions", 1, "lifecycle", "scoring",
        _USER, sm,
    )
    assert result["lifecycle"] == "scoring"


# ── Dict shape with scope but caller lacks it ──

@pytest.mark.asyncio
async def test_dict_shape_scope_check_still_enforces():
    storage = _FakeStorage({"id": 1, "lifecycle": "scenario"})
    sm = _make_sm_lookup({
        ("scenario", "scoring"): {
            "required_scope": "admin",
            "condition_expr": None,
        },
    })
    with pytest.raises(TerminScopeError):
        await do_state_transition(
            storage, "sessions", 1, "lifecycle", "scoring",
            _USER, sm,  # _USER only has "play"
        )


# ── CEL eval context exposes singular alias + the_user + record ──

@pytest.mark.asyncio
async def test_cel_eval_context_shape():
    """The CEL context must expose the record under the singular
    name (e.g. `session.X`) AND `record.X`, plus `the_user` and
    `now`. Tests via the calls list on the fake evaluator."""
    storage = _FakeStorage({"id": 7, "lifecycle": "scenario", "field": "value"})
    sm = _make_sm_lookup({
        ("scenario", "scoring"): {
            "required_scope": "",
            "condition_expr": "session.field == 'value'",
        },
    })
    expr_eval = _FakeExprEval(return_map={"session.field == 'value'": True})
    await do_state_transition(
        storage, "sessions", 7, "lifecycle", "scoring",
        _USER, sm, expr_eval=expr_eval,
    )
    ctx = expr_eval.calls[0]["ctx"]
    # singular alias
    assert "session" in ctx
    assert ctx["session"]["field"] == "value"
    # generic record alias
    assert "record" in ctx
    assert ctx["record"]["field"] == "value"
    # principal + now
    assert "the_user" in ctx
    assert ctx["the_user"]["id"] == "u1"
    assert "now" in ctx
