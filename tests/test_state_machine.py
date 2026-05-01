# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Direct unit tests for the state-machine engine in
``termin_core.state.machine``.

The conformance suite exercises this module via the FastAPI bridge;
these tests target the engine's decision paths in isolation —
unknown table / machine, missing record, undeclared transitions,
scope gates, atomic CAS races, and the BRD #3 §5 transition-event
publication shape.

Coverage target: bring state/machine.py from 0% to 90%+.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from termin_core.errors import (
    TerminBadRequestError,
    TerminConflictError,
    TerminNotFoundError,
    TerminScopeError,
)
from termin_core.providers.storage_contract import (
    Eq,
    UpdateResult,
)
from termin_core.state.machine import do_state_transition


# ── Fakes ──


class _FakeStorage:
    """Records every call; ``update_if`` honors the condition predicate."""

    def __init__(
        self,
        records: dict[tuple[str, int], dict] | None = None,
        *,
        cas_outcomes: dict[tuple[str, int], list[UpdateResult]] | None = None,
    ) -> None:
        self._records = dict(records or {})
        self._cas_outcomes = {k: list(v) for k, v in (cas_outcomes or {}).items()}

    async def read(self, table, record_id):
        rec = self._records.get((table, record_id))
        return dict(rec) if rec else None

    async def update_if(self, table, record_id, *, condition, patch):
        # Custom-scripted outcome (e.g., concurrent-CAS-failure tests).
        scripted = self._cas_outcomes.get((table, record_id))
        if scripted:
            return scripted.pop(0)
        # Default: apply iff condition matches current state.
        rec = self._records.get((table, record_id))
        if rec is None:
            return UpdateResult(
                applied=False, reason="not_found", record=None,
            )
        current = rec.get(condition.field, "") if isinstance(condition, Eq) else ""
        if isinstance(condition, Eq) and current != condition.value:
            return UpdateResult(
                applied=False,
                reason="condition_failed",
                record=dict(rec),
            )
        new = {**rec, **patch}
        self._records[(table, record_id)] = new
        return UpdateResult(applied=True, reason="applied", record=dict(new))


class _StubTerminator:
    def __init__(self) -> None:
        self.routed: list = []

    def route(self, err) -> None:
        self.routed.append(err)


class _StubEventBus:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, event):
        self.published.append(dict(event))


def _user(scopes=(), pid: str = "alice") -> dict:
    """Build a v0.9-shaped user dict the state engine accepts."""
    return {
        "the_user": {
            "id": pid,
            "display_name": pid.title(),
            "is_anonymous": False,
            "is_system": False,
            "scopes": list(scopes),
        },
        "scopes": list(scopes),
        "role": "tester",
    }


def _sm(transitions: dict[tuple[str, str], str | None]) -> dict:
    """Build a single state-machine dict the engine reads."""
    return {
        "machine_name": "lifecycle",
        "column": "lifecycle",
        "initial": "draft",
        "transitions": transitions,
    }


# ── Pre-flight failures ──


class TestPreflightChecks:
    def test_unknown_table_raises_400(self):
        storage = _FakeStorage()
        with pytest.raises(TerminBadRequestError):
            asyncio.run(do_state_transition(
                storage, "ghost_table", 1, "lifecycle", "active",
                _user(), {},
            ))

    def test_unknown_machine_raises_400(self):
        storage = _FakeStorage()
        sms = {"products": [_sm({("draft", "active"): None})]}
        with pytest.raises(TerminBadRequestError):
            asyncio.run(do_state_transition(
                storage, "products", 1, "ghost_machine", "active",
                _user(), sms,
            ))

    def test_missing_record_raises_404(self):
        storage = _FakeStorage()  # no records
        sms = {"products": [_sm({("draft", "active"): None})]}
        with pytest.raises(TerminNotFoundError):
            asyncio.run(do_state_transition(
                storage, "products", 1, "lifecycle", "active",
                _user(), sms,
            ))


# ── Transition table validation ──


class TestUndeclaredTransitions:
    def test_undeclared_transition_raises_409_with_states(self):
        storage = _FakeStorage(records={
            ("products", 1): {"id": 1, "lifecycle": "active"},
        })
        sms = {"products": [_sm({("draft", "active"): None})]}
        with pytest.raises(TerminConflictError) as exc:
            asyncio.run(do_state_transition(
                storage, "products", 1, "lifecycle", "draft",
                _user(), sms,
            ))
        assert "active" in str(exc.value)
        assert "draft" in str(exc.value)
        assert exc.value.extra["from_state"] == "active"
        assert exc.value.extra["to_state"] == "draft"

    def test_undeclared_transition_routes_to_terminator(self):
        storage = _FakeStorage(records={
            ("products", 1): {"id": 1, "lifecycle": "active"},
        })
        sms = {"products": [_sm({("draft", "active"): None})]}
        term = _StubTerminator()
        with pytest.raises(TerminConflictError):
            asyncio.run(do_state_transition(
                storage, "products", 1, "lifecycle", "draft",
                _user(), sms, terminator=term,
            ))
        assert len(term.routed) == 1
        assert term.routed[0].kind == "state"


# ── Scope gating ──


class TestScopeGate:
    def test_missing_required_scope_raises_403(self):
        storage = _FakeStorage(records={
            ("products", 1): {"id": 1, "lifecycle": "draft"},
        })
        sms = {"products": [_sm({("draft", "active"): "products.publish"})]}
        with pytest.raises(TerminScopeError):
            asyncio.run(do_state_transition(
                storage, "products", 1, "lifecycle", "active",
                _user(scopes=()), sms,
            ))

    def test_present_scope_passes(self):
        storage = _FakeStorage(records={
            ("products", 1): {"id": 1, "lifecycle": "draft"},
        })
        sms = {"products": [_sm({("draft", "active"): "products.publish"})]}
        result = asyncio.run(do_state_transition(
            storage, "products", 1, "lifecycle", "active",
            _user(scopes=("products.publish",)), sms,
        ))
        assert result["lifecycle"] == "active"

    def test_no_scope_required_passes_for_anyone(self):
        storage = _FakeStorage(records={
            ("products", 1): {"id": 1, "lifecycle": "draft"},
        })
        sms = {"products": [_sm({("draft", "active"): None})]}
        result = asyncio.run(do_state_transition(
            storage, "products", 1, "lifecycle", "active",
            _user(scopes=()), sms,
        ))
        assert result["lifecycle"] == "active"


# ── Atomic CAS race handling ──


class TestCasRace:
    def test_cas_not_found_after_read_raises_404(self):
        storage = _FakeStorage(
            records={("products", 1): {"id": 1, "lifecycle": "draft"}},
            cas_outcomes={("products", 1): [
                UpdateResult(applied=False, reason="not_found", record=None),
            ]},
        )
        sms = {"products": [_sm({("draft", "active"): None})]}
        with pytest.raises(TerminNotFoundError):
            asyncio.run(do_state_transition(
                storage, "products", 1, "lifecycle", "active",
                _user(), sms,
            ))

    def test_cas_condition_failed_surfaces_post_race_state(self):
        storage = _FakeStorage(
            records={("products", 1): {"id": 1, "lifecycle": "draft"}},
            cas_outcomes={("products", 1): [UpdateResult(
                applied=False,
                reason="condition_failed",
                record={"id": 1, "lifecycle": "shipped"},
            )]},
        )
        sms = {"products": [_sm({("draft", "active"): None})]}
        with pytest.raises(TerminConflictError) as exc:
            asyncio.run(do_state_transition(
                storage, "products", 1, "lifecycle", "active",
                _user(), sms,
            ))
        assert exc.value.extra["current_state"] == "shipped"


# ── Self-transitions are valid when declared ──


class TestSelfTransition:
    def test_declared_self_transition_succeeds(self):
        storage = _FakeStorage(records={
            ("products", 1): {"id": 1, "lifecycle": "active"},
        })
        sms = {"products": [_sm({("active", "active"): None})]}
        result = asyncio.run(do_state_transition(
            storage, "products", 1, "lifecycle", "active",
            _user(), sms,
        ))
        assert result["lifecycle"] == "active"


# ── BRD #3 §5 transition-event publishing ──


class TestEventPublication:
    def test_publishes_three_events_on_success(self):
        storage = _FakeStorage(records={
            ("products", 1): {"id": 1, "lifecycle": "draft"},
        })
        sms = {"products": [_sm({("draft", "active"): None})]}
        bus = _StubEventBus()
        asyncio.run(do_state_transition(
            storage, "products", 1, "lifecycle", "active",
            _user(), sms, event_bus=bus,
        ))
        # Per BRD #3 §5.1: exited + entered events; legacy
        # content.<X>.updated event for v0.8 subscribers.
        channels = [e["channel_id"] for e in bus.published]
        assert "products.lifecycle.draft.exited" in channels
        assert "products.lifecycle.active.entered" in channels
        assert "content.products.updated" in channels

    def test_event_payload_carries_brd_shape(self):
        storage = _FakeStorage(records={
            ("products", 1): {"id": 1, "lifecycle": "draft"},
        })
        sms = {"products": [_sm({("draft", "active"): None})]}
        bus = _StubEventBus()
        asyncio.run(do_state_transition(
            storage, "products", 1, "lifecycle", "active",
            _user(scopes=("publish",)), sms, event_bus=bus,
        ))
        # The exited/entered events carry the typed payload.
        exited = next(
            e for e in bus.published
            if e["channel_id"] == "products.lifecycle.draft.exited"
        )
        payload = exited["data"]
        assert payload["from_state"] == "draft"
        assert payload["to_state"] == "active"
        assert payload["record_id"] == 1
        assert payload["trigger_kind"] == "user_action"
        # on_behalf_of and invoked_by equal for direct user actions.
        assert payload["on_behalf_of"] == payload["invoked_by"]
        assert payload["on_behalf_of"]["id"] == "alice"
        assert "publish" in payload["on_behalf_of"]["scopes"]

    def test_no_events_when_no_event_bus(self):
        storage = _FakeStorage(records={
            ("products", 1): {"id": 1, "lifecycle": "draft"},
        })
        sms = {"products": [_sm({("draft", "active"): None})]}
        # No event_bus argument — transition still succeeds, just no
        # events published.
        result = asyncio.run(do_state_transition(
            storage, "products", 1, "lifecycle", "active",
            _user(), sms, event_bus=None,
        ))
        assert result["lifecycle"] == "active"


# ── Multi-state-machine independence ──


class TestMultipleStateMachines:
    def test_routes_to_correct_machine_by_name(self):
        storage = _FakeStorage(records={
            ("documents", 1): {"id": 1, "lifecycle": "draft", "approval": "pending"},
        })
        sms = {"documents": [
            _sm({("draft", "published"): None}),  # lifecycle (default name)
            {
                "machine_name": "approval",
                "column": "approval",
                "initial": "pending",
                "transitions": {("pending", "approved"): "docs.approve"},
            },
        ]}
        # Drive the approval machine specifically — lifecycle stays.
        result = asyncio.run(do_state_transition(
            storage, "documents", 1, "approval", "approved",
            _user(scopes=("docs.approve",)), sms,
        ))
        assert result["approval"] == "approved"
        assert result["lifecycle"] == "draft"  # untouched
