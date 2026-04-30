# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Pure WebSocket multiplexer dispatch loop.

Slice 7.2.f extracts the per-frame message handling from
``termin_runtime/websocket_manager.register_websocket_routes`` into
:func:`dispatch_websocket_session`, a coroutine that operates on a
:class:`~termin_core.routing.websocket.TerminWebSocket` plus a runtime
context. Adapters provide the framework-side route shell; everything
above the wire lives here.

Frame protocol (unchanged from v0.9 termin.js):

* Inbound: ``{"v":1,"ch":<topic>,"op":<op>,"ref":<correlation>}``
  where ``op`` is ``subscribe``, ``unsubscribe``, or ``request``.
* Outbound: ``{"v":1,"ch":<topic>,"op":<op>,"ref":<correlation>,
  "payload":<data>}`` where ``op`` is ``response``, ``push``, or
  ``error``.

Storage reads are delegated through ``ctx`` hooks rather than direct
imports — the same ctx-stash pattern slice 7.2.e introduced for CRUD
handlers. The dispatcher needs exactly one hook:
``await ctx.list_records_for_ws(content_name) -> list[dict]``. The
runtime stashes a closure over its storage layer at startup; an
alternate runtime supplies its own. The dispatcher applies the
ownership cascade before responding via
:func:`~connection_manager.filter_owned_rows`.
"""

from __future__ import annotations

from typing import Any

from .connection_manager import filter_owned_rows
from .websocket import TerminWebSocket


def _identity_frame(user: Any) -> dict:
    """Build the first push frame the dispatcher sends after accept.

    Carries role / scopes / profile so the client can render the
    correct nav-chrome before any subscriptions land. Works for
    legacy dict-shaped users (the v0.9 reference runtime) and for
    bare anonymous (no ``user`` at all from a future identity-less
    adapter).
    """
    if not isinstance(user, dict):
        return {
            "v": 1,
            "ch": "runtime.identity",
            "op": "push",
            "ref": None,
            "payload": {"role": "", "scopes": [], "profile": {}},
        }
    return {
        "v": 1,
        "ch": "runtime.identity",
        "op": "push",
        "ref": None,
        "payload": {
            "role": user.get("role", ""),
            "scopes": list(user.get("scopes", []) or []),
            "profile": user.get("profile", {}),
        },
    }


def _content_name_from_channel(ch: str) -> str | None:
    """Same parser as :mod:`connection_manager`. Returns the
    snake-case content name when the topic is content-shaped, else
    None — which short-circuits the storage read.
    """
    parts = ch.split(".")
    if len(parts) >= 2 and parts[0] == "content":
        return parts[1]
    return None


async def _load_initial_rows(
    ctx: Any, content_name: str, user: Any,
) -> list[dict]:
    """Fetch the rows the client should see for this content topic
    and apply the ownership cascade.

    The runtime supplies ``ctx.list_records_for_ws`` — a closure over
    its storage layer that returns all records for the content type.
    The cascade trims rows the user doesn't own; non-owned content
    passes through untouched.
    """
    rows = await ctx.list_records_for_ws(content_name)
    owner_field = ctx.conn_manager.get_ownership_field(content_name)
    return filter_owned_rows(rows, owner_field, user)


async def dispatch_websocket_session(
    ws: TerminWebSocket,
    ctx: Any,
    user: Any,
) -> None:
    """Run a single WebSocket session to completion.

    The adapter calls this after ``ws.accept()`` and after resolving
    ``user`` from the framework's auth surface (cookies on FastAPI;
    headers on others). The coroutine returns when the client
    disconnects or any frame raises — the adapter is responsible for
    catching framework-specific disconnect exceptions and calling
    :meth:`~ConnectionManager.disconnect` on the conn_id.

    Returns the conn_id so the adapter's ``finally`` block can clean
    up the registry.
    """
    conn_id = await ctx.conn_manager.connect(ws, user)

    # First frame: identity push so the client renders chrome before
    # any subscription frames land.
    await ws.send_json(_identity_frame(user))

    while True:
        frame = await ws.receive_json()
        op = frame.get("op", "")
        ch = frame.get("ch", "")
        ref = frame.get("ref")

        if op == "subscribe":
            ctx.conn_manager.add_subscription(conn_id, ch)
            content_name = _content_name_from_channel(ch)
            if content_name:
                try:
                    rows = await _load_initial_rows(ctx, content_name, user)
                    await ws.send_json({
                        "v": 1, "ch": ch, "op": "response", "ref": ref,
                        "payload": {"current": rows},
                    })
                except Exception as e:
                    await ws.send_json({
                        "v": 1, "ch": ch, "op": "error", "ref": ref,
                        "payload": {"message": str(e)},
                    })
            else:
                await ws.send_json({
                    "v": 1, "ch": ch, "op": "response", "ref": ref,
                    "payload": {"current": []},
                })

        elif op == "unsubscribe":
            ctx.conn_manager.remove_subscription(conn_id, ch)
            await ws.send_json({
                "v": 1, "ch": ch, "op": "response", "ref": ref,
                "payload": {"unsubscribed": True},
            })

        elif op == "request":
            content_name = _content_name_from_channel(ch)
            if content_name:
                try:
                    rows = await _load_initial_rows(ctx, content_name, user)
                    await ws.send_json({
                        "v": 1, "ch": ch, "op": "response", "ref": ref,
                        "payload": {"data": rows},
                    })
                except Exception as e:
                    await ws.send_json({
                        "v": 1, "ch": ch, "op": "error", "ref": ref,
                        "payload": {"message": str(e)},
                    })


__all__ = [
    "dispatch_websocket_session",
]
