# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Append CRUD verb handler (v0.9.3 extraction of v0.9.2's L3 append).

Framework-free implementation of the append verb. The shape is the
same as ``_do_append`` in ``termin_server/routes.py`` (which is
deleted as part of this slice). Storage access goes through
``ctx.storage`` (StorageProvider Protocol) rather than the aiosqlite-
specific helpers in ``termin_server.storage``.

Two transports call this:
  - REST: ``POST <resource>/{id}/<field>:append`` — wrapped by a
    framework adapter that maps ``AppendValidationError`` →
    HTTP 400 and ``AppendNotFoundError`` → HTTP 404.
  - WebSocket: the ``{type: "append"}`` frame on the per-page WS —
    the dispatcher in ``channel_dispatch`` translates the same
    exception classes into structured error frames.

Both transports map onto the same shared helper so they cannot
drift apart on validation, ownership, or audit semantics.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


# v0.9.2 L3: canonical conversation entry kinds (per tech-design §7.2).
# v0.9.2 close-out (2026-05-05): the canonical AI-side kind is
# `agent` (renamed from `assistant` per BRD/D-01 framing — see Tenet
# 5: declared agents over ambient agents). `assistant` is preserved
# as a back-compat read shape; new appends use `agent`.
CANONICAL_KINDS = frozenset({
    "user", "agent", "assistant", "tool_call", "tool_result",
    "system_event",
})


class AppendValidationError(Exception):
    """Body shape problem (invalid kind, missing body, malformed JSON).

    REST adapters map to HTTP 400; WS dispatchers map to a
    ``validation_error`` frame.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AppendNotFoundError(Exception):
    """Parent record doesn't exist or row-filter excludes it.

    REST adapters map to HTTP 404; WS dispatchers map to a
    ``not_found`` frame. The same status used for both
    "record-doesn't-exist" and "you-don't-own-this-record" so
    ownership doesn't leak existence (BRD #3 §3.7).
    """

    def __init__(self, message: str = "Not found") -> None:
        super().__init__(message)
        self.message = message


def _uuid7_str() -> str:
    """Generate a UUIDv7 string (time-ordered, suitable for entry ids)."""
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    high = (ts_ms << 16) | (0x7 << 12) | rand_a
    low = (0b10 << 62) | rand_b
    return str(uuid.UUID(int=(high << 64) | low))


async def append_to_field(
    ctx,
    *,
    content_ref: str,
    key_val: Any,
    field_name: str,
    payload: Mapping[str, Any],
    user: Optional[Mapping[str, Any]] = None,
    row_filter: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Append a new entry to a conversation/structured-list field.

    Validates the payload, loads the parent record (404 if absent or
    if a row_filter excludes it), reads the existing JSON column,
    builds the new entry with canonical metadata, writes the updated
    list back via ``ctx.storage.update``, and publishes the
    ``content.<name>.<field>.appended`` event so listener computes
    and WS subscribers fire.

    Args:
        ctx: RuntimeContext with ``storage`` (StorageProvider),
            ``event_bus`` (optional), ``terminator``, and
            ``run_event_handlers`` (optional).
        content_ref: snake_case content type name.
        key_val: primary key of the parent record.
        field_name: field on the parent that holds the JSON list.
        payload: incoming append body (kind, body, optional metadata).
        user: principal making the append (the_user dict shape).
        row_filter: optional ownership predicate
            (``{kind: "ownership", field: <owner_field>}``).

    Returns:
        The new entry dict on success.

    Raises:
        AppendValidationError: payload shape problem.
        AppendNotFoundError: record absent or row-filter rejected.
    """
    if not key_val:
        raise AppendValidationError("Missing record id")
    if not isinstance(payload, Mapping):
        raise AppendValidationError("Body must be a JSON object")

    kind = payload.get("kind", "")
    if kind not in CANONICAL_KINDS:
        raise AppendValidationError(
            f"Invalid kind '{kind}'. Must be one of: {sorted(CANONICAL_KINDS)}"
        )
    body_text = payload.get("body")
    if body_text is None or body_text == "":
        raise AppendValidationError("body is required")

    record = await ctx.storage.read(content_ref, key_val)
    if record is None:
        raise AppendNotFoundError("Not found")

    # Row filter: their_own ownership check on the parent record.
    if row_filter and row_filter.get("kind") == "ownership":
        user_id = (user or {}).get("id") if user else None
        if not user_id and isinstance(user, Mapping):
            the_user = user.get("the_user") or {}
            user_id = the_user.get("id")
        owner_field = row_filter.get("field")
        if owner_field and record.get(owner_field) != user_id:
            raise AppendNotFoundError("Not found")

    # Read existing entries. Storage providers may return the field
    # as either a native Python list (DynamoDB, Postgres JSONB, any
    # backend that natively persists list-typed columns) or as a
    # JSON-text string (the SQLite reference runtime uses a TEXT
    # column holding the JSON encoding). Issue #5: we must handle
    # both shapes so this helper is genuinely storage-Protocol
    # agnostic. Malformed / non-list values fall through to a fresh
    # list, preserving pre-fix resilience semantics.
    raw = record.get(field_name)
    if isinstance(raw, list):
        entries = list(raw)
    elif raw in (None, ""):
        entries = []
    else:
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            entries = []
        else:
            entries = decoded if isinstance(decoded, list) else []

    # Build the new entry with canonical metadata.
    user_dict = user or {}
    appender_id = user_dict.get("id", "")
    if not appender_id and isinstance(user_dict, Mapping):
        the_user = user_dict.get("the_user") or {}
        appender_id = the_user.get("id", "")
    entry = {
        "id": _uuid7_str(),
        "kind": kind,
        "body": body_text,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "appended_by_principal_id": appender_id,
    }
    # Optional per-kind metadata fields pass through unchanged.
    for k in ("type", "source", "tool_call_id", "parent_id", "tool_name",
              "tool_args", "attachments", "purpose"):
        if k in payload:
            entry[k] = payload[k]

    entries.append(entry)
    # Issue #5: pass the native Python list to ctx.storage.update.
    # Each storage provider knows how to persist its types — SQLite
    # wraps lists in json.dumps inside its update() implementation;
    # DynamoDB stores the List natively; Postgres uses JSONB. The
    # framework-free routing layer must not assume any particular
    # serialization.
    updated_record = await ctx.storage.update(
        content_ref, key_val,
        {field_name: entries},
    )

    # v0.9.2 L5: publish `content.<name>.<field>.appended`.
    if getattr(ctx, "event_bus", None) is not None:
        envelope = {
            "type": f"{content_ref}_{field_name}_appended",
            "channel_id": f"content.{content_ref}.{field_name}.appended",
            "content_name": content_ref,
            "field_name": field_name,
            "record_id": key_val,
            "record": updated_record,
            "appended_entry": entry,
            "triggered_at": entry["created_at"],
            "invoked_by_principal_id": entry["appended_by_principal_id"],
            "trigger_kind": "crud-append",
        }
        envelope["data"] = dict(envelope)
        await ctx.event_bus.publish(envelope)

    # v0.9.2 L5/L8: dispatch listener computes (When-rules + Trigger
    # on event "<field>.appended" computes) — same path the per-CRUD
    # create/update/delete dispatch uses.
    if hasattr(ctx, "run_event_handlers"):
        # Note: v0.9.2 server-side passed ``db`` as the first arg;
        # alt runtimes that don't have an aiosqlite connection should
        # have ``run_event_handlers`` accept ``None`` or stash their
        # own connection. The reference runtime adapter wraps this.
        db = getattr(ctx, "_default_db", None)
        await ctx.run_event_handlers(
            db, content_ref, f"{field_name}.appended", updated_record,
            appended_entry=entry,
            invoked_by_principal_id=entry.get("appended_by_principal_id"),
        )

    return entry


__all__ = [
    "CANONICAL_KINDS",
    "AppendValidationError",
    "AppendNotFoundError",
    "append_to_field",
]
