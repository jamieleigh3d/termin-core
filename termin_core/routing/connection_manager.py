# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Pure WebSocket connection registry + ownership-cascade fanout.

Slice 7.2.f extracts this from ``termin_runtime/websocket_manager.py``.
The original file imported ``fastapi.WebSocket`` for type hints and
nothing else; the runtime contract is the
:class:`~termin_core.routing.websocket.TerminWebSocket` Protocol.
Adapters wrap their framework's WS type to satisfy the Protocol.

The manager is responsible for three things:

1. **Connection registry** — track open sockets, their authenticated
   user dict, and the topic patterns they've subscribed to.
2. **Ownership cascade gate** — BRD #3 §3.6: subscribers to channels
   carrying owned content only receive rows whose owning principal
   matches theirs. The mapping (snake-case content name → ownership
   field) is supplied at startup by the runtime.
3. **Fanout** — :meth:`broadcast_to_subscribers` walks active
   connections, applies prefix-matching against subscription patterns,
   passes the ownership gate, and pushes the event frame.

Storage reads (initial-data load on subscribe / request) are NOT this
manager's concern — they live in :mod:`channel_dispatch`, which uses
ctx hooks so the storage backend stays out of core.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional


def _principal_id_of(user: Any) -> str:
    """Return the principal id for a runtime user dict, or ``""`` for
    anonymous / system-with-empty-id callers.

    Mirrors the shape ``identity._build_user_dict`` produces in the
    reference runtime; identity-providers in alternate runtimes that
    follow the same shape get the cascade for free. Non-dict input
    (e.g., a future where identity is set as an :class:`AuthContext`
    on the connection) returns ``""`` — callers handle the
    "principal not yet wired" case the same as anonymous.
    """
    the_user = user.get("the_user") if isinstance(user, dict) else None
    if isinstance(the_user, dict):
        if the_user.get("is_anonymous"):
            return ""
        return str(the_user.get("id") or "")
    return ""


def filter_owned_rows(
    rows: list[dict],
    ownership_field: Optional[str],
    user: Any,
) -> list[dict]:
    """Pre-filter a record list by ownership for a given user.

    Used by the initial-data load on subscribe and by the explicit
    ``request`` op in :mod:`channel_dispatch`. Pass-through when the
    content has no ownership; empty list when the principal is
    anonymous on owned content (BRD #3 §3.6 — owned content never
    leaks to anonymous subscribers).
    """
    if not ownership_field:
        return rows
    pid = _principal_id_of(user)
    if not pid:
        return []
    return [r for r in rows if r.get(ownership_field) == pid]


def _content_name_from_channel_id(channel_id: str) -> Optional[str]:
    """Extract the snake-case content name from a channel id of shape
    ``content.<X>.<verb>`` (or just ``content.<X>``).

    Returns None for non-content channels (state-machine transitions,
    custom events, internal subsystems) so the cascade leaves them
    alone — those topics aren't gated by record ownership.
    """
    if not channel_id.startswith("content."):
        return None
    parts = channel_id.split(".")
    if len(parts) < 2:
        return None
    return parts[1]


class ConnectionManager:
    """In-process registry of active WebSocket connections.

    Pure: no framework imports, no IO. The framework adapter creates
    one of these (typically once per app) and hands ``TerminWebSocket``
    instances to :meth:`connect`. Storage, identity, and the event bus
    are all the adapter's concern.

    The manager is intentionally simple — single-process, in-memory.
    Distributed runtimes that need a shared subscription registry
    (Redis pub/sub, NATS, etc.) implement their own backing without
    this class; the dispatch loop in :mod:`channel_dispatch` operates
    on whatever satisfies its small attribute surface.
    """

    def __init__(self) -> None:
        # conn_id -> {ws, user, subscriptions}
        self.active: dict[str, dict] = {}
        # snake-case content name -> ownership field column (BRD #3 §3.6)
        self._content_ownership: dict[str, str] = {}

    def set_content_ownership(self, mapping: dict[str, str]) -> None:
        """Register the per-content ownership-field lookup.

        Keyed by snake-case content name; value is the snake-case
        column carrying the owning principal id. Content not in the
        mapping has no ownership cascade applied.
        """
        self._content_ownership = dict(mapping or {})

    def get_ownership_field(self, content_name: str) -> Optional[str]:
        """Public accessor for the ownership-field lookup.

        Used by :mod:`channel_dispatch` to apply
        :func:`filter_owned_rows` on initial-data loads.
        """
        return self._content_ownership.get(content_name)

    async def connect(self, ws: Any, user: Any) -> str:
        """Register a new connection. Returns the conn_id.

        The adapter calls ``ws.accept()`` itself before handing the
        socket here — registry doesn't speak the wire. ``user`` is
        whatever the runtime's identity layer produces; the manager
        only inspects it through :func:`_principal_id_of`.
        """
        conn_id = str(uuid.uuid4())[:8]
        self.active[conn_id] = {"ws": ws, "user": user, "subscriptions": set()}
        return conn_id

    def disconnect(self, conn_id: str) -> None:
        """Drop a connection from the registry. No-op if unknown."""
        self.active.pop(conn_id, None)

    def add_subscription(self, conn_id: str, channel_id: str) -> None:
        if conn_id in self.active:
            self.active[conn_id]["subscriptions"].add(channel_id)

    def remove_subscription(self, conn_id: str, channel_id: str) -> None:
        if conn_id in self.active:
            self.active[conn_id]["subscriptions"].discard(channel_id)

    def _should_deliver_to(
        self, conn: dict, channel_id: str, event: dict,
    ) -> bool:
        """Ownership cascade gate (BRD #3 §3.6).

        Returns True when the connection should receive this
        broadcast. False when the channel carries owned content and
        the subscriber's principal id doesn't match the record's
        owning-field value. Non-owned content and non-content
        channels always pass.

        Conservative drops: missing payload, missing field, or a
        ``None`` owner value — better to under-share than to leak.
        """
        content_name = _content_name_from_channel_id(channel_id)
        if not content_name:
            return True
        owner_field = self._content_ownership.get(content_name)
        if not owner_field:
            return True
        payload = event.get("data") or event.get("record") or {}
        if not isinstance(payload, dict):
            return False
        owner_value = payload.get(owner_field)
        if owner_value is None:
            return False
        return owner_value == _principal_id_of(conn["user"])

    async def broadcast_to_subscribers(
        self, channel_id: str, event: dict,
    ) -> None:
        """Fan out an event to every subscriber whose pattern is a
        prefix of ``channel_id``.

        Prefix-matching is the topic model: a subscription to
        ``content.products`` receives every
        ``content.products.<verb>`` push. Each connection contributes
        at most one frame per broadcast — the inner ``break`` after
        a match prevents double-delivery to a connection that has
        both ``content.products`` and ``content.products.created``
        subscribed simultaneously.

        Connections whose ``send_json`` raises are dropped from the
        registry — typical case is a closed socket whose disconnect
        handler hasn't fired yet.
        """
        dead: list[str] = []
        for conn_id, conn in self.active.items():
            for pattern in conn["subscriptions"]:
                if channel_id.startswith(pattern):
                    if not self._should_deliver_to(conn, channel_id, event):
                        break
                    try:
                        await conn["ws"].send_json({
                            "v": 1,
                            "ch": channel_id,
                            "op": "push",
                            "ref": None,
                            "payload": (
                                event.get("data")
                                or event.get("record")
                                or event
                            ),
                        })
                    except Exception:
                        dead.append(conn_id)
                    break
        for conn_id in dead:
            self.disconnect(conn_id)


__all__ = [
    "ConnectionManager",
    "filter_owned_rows",
]
