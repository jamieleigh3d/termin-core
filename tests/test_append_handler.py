# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Direct unit tests for ``termin_core.routing.append.append_to_field``.

The handler is exercised end-to-end in termin-conformance via the v0.9.2
conversation-field pack. These tests target the storage-Protocol contract
directly:

  * the read path must accept native Python lists (alt runtimes whose
    storage returns native list types — DynamoDB, Postgres JSONB —
    rather than JSON-text, BRD §6.2 / Protocol contract on
    ``storage.read``);
  * the write path must pass native Python lists to ``storage.update``
    (alt runtimes' storage knows how to persist native types; SQLite
    in particular wraps lists in ``json.dumps`` on the way in);
  * the legacy SQLite-shaped JSON-string-on-read shape must keep
    working so the reference runtime is unaffected;
  * resilience on the read path (None / empty / malformed JSON) is
    preserved.

These tests pin issue #5 (filed 2026-05-08): the v0.9.3-extracted
``append_to_field`` had SQLite-specific ``json.dumps`` / ``json.loads``
hardcoded into both directions, blocking adoption by any storage
provider that stores list-typed columns natively.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from termin_core.routing.append import (
    AppendNotFoundError,
    AppendValidationError,
    append_to_field,
)


class _RecordingStorage:
    """Minimal in-memory StorageProvider stand-in for append_to_field.

    Stores records by ``(content_type, id)`` and records every
    ``update`` call's patch verbatim — tests assert on ``last_patch``
    to verify the handler passed the intended Python type (not a
    pre-serialized string).

    The constructor's ``initial`` lets the test seed the field value
    in either shape: a JSON-text string (SQLite-shaped) or a native
    Python list (DynamoDB-shaped). Both must roundtrip through
    ``append_to_field``.
    """

    def __init__(self, records=None):
        self._records = {k: dict(v) for k, v in (records or {}).items()}
        self.last_patch: dict | None = None

    async def read(self, content_type, key):
        rec = self._records.get((content_type, key))
        return dict(rec) if rec is not None else None

    async def update(self, content_type, key, patch):
        self.last_patch = dict(patch)
        rec = self._records.get((content_type, key))
        if rec is None:
            return None
        rec.update(patch)
        return dict(rec)


class _StubCtx:
    def __init__(self, storage):
        self.storage = storage
        # No event_bus, no run_event_handlers — append_to_field
        # tolerates their absence (lines 188 / 207 in append.py).


def _user(pid="alice"):
    return {"id": pid}


def _payload(body="hello"):
    return {"kind": "user", "body": body}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Read path: native list (alt-runtime shape) ──


def test_append_read_path_accepts_native_list():
    """A storage provider that returns the field as a native ``list``
    must be supported. v0.9.3 hardcoded ``json.loads(raw)``, which
    raises TypeError on a list — this test failed pre-fix."""
    initial_entries = [
        {"id": "e1", "kind": "user", "body": "first",
         "created_at": "2026-05-08T00:00:00Z",
         "appended_by_principal_id": "alice"},
    ]
    storage = _RecordingStorage(records={
        ("ticket", "t1"): {"id": "t1", "messages": initial_entries},
    })
    ctx = _StubCtx(storage)

    entry = _run(append_to_field(
        ctx,
        content_ref="ticket", key_val="t1", field_name="messages",
        payload=_payload("second"), user=_user(),
    ))

    # The handler should have appended to the existing native list and
    # passed a native list to storage.update — never a JSON string.
    assert entry["body"] == "second"
    assert entry["kind"] == "user"
    patch = storage.last_patch
    assert patch is not None
    assert isinstance(patch["messages"], list), (
        f"expected native list patch, got {type(patch['messages']).__name__}"
    )
    assert len(patch["messages"]) == 2
    assert patch["messages"][0]["body"] == "first"
    assert patch["messages"][1]["body"] == "second"


# ── Read path: legacy SQLite JSON-text shape (reference runtime) ──


def test_append_read_path_accepts_json_string():
    """SQLite-shaped storage returns the column as a JSON-text string;
    this is the legacy shape and must keep working."""
    initial_json = json.dumps([
        {"id": "e1", "kind": "user", "body": "first",
         "created_at": "2026-05-08T00:00:00Z",
         "appended_by_principal_id": "alice"},
    ])
    storage = _RecordingStorage(records={
        ("ticket", "t1"): {"id": "t1", "messages": initial_json},
    })
    ctx = _StubCtx(storage)

    entry = _run(append_to_field(
        ctx,
        content_ref="ticket", key_val="t1", field_name="messages",
        payload=_payload("second"), user=_user(),
    ))

    # Even though the input shape was a JSON string, the patch the
    # handler sends to storage.update must be a native list. The
    # SQLite provider knows how to serialize lists on its side.
    assert entry["body"] == "second"
    patch = storage.last_patch
    assert patch is not None
    assert isinstance(patch["messages"], list), (
        f"expected native list patch, got {type(patch['messages']).__name__}"
    )
    assert len(patch["messages"]) == 2


# ── Read path: edge cases (None, empty string, malformed JSON) ──


def test_append_read_path_treats_none_as_empty_list():
    """A field that was never written returns None from storage.read;
    treat as empty starting list."""
    storage = _RecordingStorage(records={
        ("ticket", "t1"): {"id": "t1", "messages": None},
    })
    ctx = _StubCtx(storage)

    entry = _run(append_to_field(
        ctx,
        content_ref="ticket", key_val="t1", field_name="messages",
        payload=_payload("first"), user=_user(),
    ))

    assert entry["body"] == "first"
    assert isinstance(storage.last_patch["messages"], list)
    assert len(storage.last_patch["messages"]) == 1


def test_append_read_path_treats_empty_string_as_empty_list():
    """SQLite legacy: empty string columns map to empty list (legacy
    update_fields semantics filter out empty strings on the way in,
    but we must tolerate them on the way out)."""
    storage = _RecordingStorage(records={
        ("ticket", "t1"): {"id": "t1", "messages": ""},
    })
    ctx = _StubCtx(storage)

    entry = _run(append_to_field(
        ctx,
        content_ref="ticket", key_val="t1", field_name="messages",
        payload=_payload("first"), user=_user(),
    ))

    assert entry["body"] == "first"
    assert isinstance(storage.last_patch["messages"], list)
    assert len(storage.last_patch["messages"]) == 1


def test_append_read_path_resilient_to_malformed_json():
    """Malformed JSON in the column starts a fresh list rather than
    crashing the append (pre-existing resilience semantics)."""
    storage = _RecordingStorage(records={
        ("ticket", "t1"): {"id": "t1", "messages": "{not json"},
    })
    ctx = _StubCtx(storage)

    entry = _run(append_to_field(
        ctx,
        content_ref="ticket", key_val="t1", field_name="messages",
        payload=_payload("recovered"), user=_user(),
    ))

    assert entry["body"] == "recovered"
    assert isinstance(storage.last_patch["messages"], list)
    assert len(storage.last_patch["messages"]) == 1


def test_append_read_path_resilient_to_non_list_json():
    """Non-list JSON value in the column (e.g. a bare object) starts
    fresh rather than crashing — preserves pre-fix resilience."""
    storage = _RecordingStorage(records={
        ("ticket", "t1"): {"id": "t1", "messages": '{"oops": true}'},
    })
    ctx = _StubCtx(storage)

    entry = _run(append_to_field(
        ctx,
        content_ref="ticket", key_val="t1", field_name="messages",
        payload=_payload("recovered"), user=_user(),
    ))

    assert entry["body"] == "recovered"
    assert isinstance(storage.last_patch["messages"], list)
    assert len(storage.last_patch["messages"]) == 1


# ── Validation surface (smoke — covered more deeply in conformance) ──


def test_append_rejects_invalid_kind():
    storage = _RecordingStorage(records={
        ("ticket", "t1"): {"id": "t1", "messages": []},
    })
    ctx = _StubCtx(storage)
    with pytest.raises(AppendValidationError):
        _run(append_to_field(
            ctx,
            content_ref="ticket", key_val="t1", field_name="messages",
            payload={"kind": "bogus", "body": "x"}, user=_user(),
        ))


def test_append_rejects_missing_body():
    storage = _RecordingStorage(records={
        ("ticket", "t1"): {"id": "t1", "messages": []},
    })
    ctx = _StubCtx(storage)
    with pytest.raises(AppendValidationError):
        _run(append_to_field(
            ctx,
            content_ref="ticket", key_val="t1", field_name="messages",
            payload={"kind": "user"}, user=_user(),
        ))


def test_append_404_when_record_absent():
    storage = _RecordingStorage(records={})
    ctx = _StubCtx(storage)
    with pytest.raises(AppendNotFoundError):
        _run(append_to_field(
            ctx,
            content_ref="ticket", key_val="missing", field_name="messages",
            payload=_payload(), user=_user(),
        ))


def test_append_ownership_filter_404_when_owner_mismatch():
    storage = _RecordingStorage(records={
        ("ticket", "t1"): {"id": "t1", "owner_id": "alice", "messages": []},
    })
    ctx = _StubCtx(storage)
    with pytest.raises(AppendNotFoundError):
        _run(append_to_field(
            ctx,
            content_ref="ticket", key_val="t1", field_name="messages",
            payload=_payload(), user=_user("bob"),
            row_filter={"kind": "ownership", "field": "owner_id"},
        ))
