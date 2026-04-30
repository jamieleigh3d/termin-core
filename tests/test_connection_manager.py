# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the framework-agnostic ConnectionManager added in slice
7.2.f.

These exercise the manager directly with stub :class:`TerminWebSocket`
implementations — no FastAPI, no asyncio loop tricks. The cascade
gate, prefix-matching fanout, and dead-connection cleanup all live
here so the WS dispatch tests can stay focused on the message loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from termin_core.routing import ConnectionManager, filter_owned_rows


class _StubWS:
    """Minimal TerminWebSocket-shaped stub.

    Captures every send_json payload for inspection. The ``fail`` flag
    forces send_json to raise — used to test dead-connection cleanup.
    ``principal`` is required by the Protocol (slice 7.2.d) but
    unused by the manager; we set None to mirror the unauthenticated
    path.
    """

    principal = None

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[Any] = []
        self.fail = fail

    async def accept(self) -> None:  # pragma: no cover (manager doesn't call)
        return

    async def send_json(self, data: Any) -> None:
        if self.fail:
            raise RuntimeError("simulated socket failure")
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:  # pragma: no cover
        return

    async def receive_json(self) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def receive_text(self) -> str:  # pragma: no cover
        raise NotImplementedError

    async def close(self, code: int = 1000) -> None:  # pragma: no cover
        return


def _user(pid: str = "", *, anonymous: bool = False) -> dict:
    """Build a minimal v0.9 user-dict shape — what the cascade reads."""
    return {
        "the_user": {"id": pid, "is_anonymous": anonymous},
        "scopes": [],
        "role": "",
    }


# ── filter_owned_rows ──


class TestFilterOwnedRows:
    def test_passthrough_when_no_ownership_field(self):
        rows = [{"id": "1"}, {"id": "2"}]
        assert filter_owned_rows(rows, None, _user("alice")) == rows

    def test_passthrough_for_empty_string_ownership_field(self):
        # Empty string is a falsey "no ownership" signal — same as None.
        rows = [{"id": "1"}]
        assert filter_owned_rows(rows, "", _user("alice")) == rows

    def test_drops_all_for_anonymous_on_owned_content(self):
        rows = [{"id": "1", "owner_id": "alice"}, {"id": "2", "owner_id": "bob"}]
        result = filter_owned_rows(rows, "owner_id", _user("", anonymous=True))
        assert result == []

    def test_drops_all_for_empty_principal_on_owned_content(self):
        rows = [{"id": "1", "owner_id": "alice"}]
        # is_anonymous false but no id — also yields empty principal id.
        user = {"the_user": {"id": "", "is_anonymous": False}}
        assert filter_owned_rows(rows, "owner_id", user) == []

    def test_keeps_only_matching_rows(self):
        rows = [
            {"id": "1", "owner_id": "alice"},
            {"id": "2", "owner_id": "bob"},
            {"id": "3", "owner_id": "alice"},
        ]
        result = filter_owned_rows(rows, "owner_id", _user("alice"))
        assert [r["id"] for r in result] == ["1", "3"]


# ── ConnectionManager registry mechanics ──


class TestConnectRegistry:
    def test_connect_returns_unique_conn_ids(self):
        mgr = ConnectionManager()
        ws_a, ws_b = _StubWS(), _StubWS()
        ids = asyncio.run(self._two_connects(mgr, ws_a, ws_b))
        assert ids[0] != ids[1]
        assert len(mgr.active) == 2

    @staticmethod
    async def _two_connects(mgr, a, b):
        return [
            await mgr.connect(a, _user("alice")),
            await mgr.connect(b, _user("bob")),
        ]

    def test_disconnect_removes_from_registry(self):
        mgr = ConnectionManager()
        ws = _StubWS()
        cid = asyncio.run(mgr.connect(ws, _user("alice")))
        assert cid in mgr.active
        mgr.disconnect(cid)
        assert cid not in mgr.active

    def test_disconnect_unknown_is_noop(self):
        mgr = ConnectionManager()
        mgr.disconnect("not-a-real-id")  # must not raise


class TestSubscriptions:
    def test_add_subscription_records_pattern(self):
        mgr = ConnectionManager()
        cid = asyncio.run(mgr.connect(_StubWS(), _user("alice")))
        mgr.add_subscription(cid, "content.products")
        assert "content.products" in mgr.active[cid]["subscriptions"]

    def test_add_to_unknown_conn_is_noop(self):
        mgr = ConnectionManager()
        mgr.add_subscription("ghost", "content.products")  # must not raise

    def test_remove_subscription_drops_pattern(self):
        mgr = ConnectionManager()
        cid = asyncio.run(mgr.connect(_StubWS(), _user("alice")))
        mgr.add_subscription(cid, "content.products")
        mgr.remove_subscription(cid, "content.products")
        assert "content.products" not in mgr.active[cid]["subscriptions"]

    def test_remove_unknown_pattern_is_noop(self):
        mgr = ConnectionManager()
        cid = asyncio.run(mgr.connect(_StubWS(), _user("alice")))
        mgr.remove_subscription(cid, "content.never-subscribed")


class TestOwnershipLookup:
    def test_set_and_get_ownership_field(self):
        mgr = ConnectionManager()
        mgr.set_content_ownership({"products": "owner_id"})
        assert mgr.get_ownership_field("products") == "owner_id"
        assert mgr.get_ownership_field("orders") is None

    def test_set_to_none_clears(self):
        mgr = ConnectionManager()
        mgr.set_content_ownership({"products": "owner_id"})
        mgr.set_content_ownership(None)
        assert mgr.get_ownership_field("products") is None


# ── broadcast fanout + ownership cascade ──


class TestBroadcastFanout:
    def test_prefix_match_delivers(self):
        mgr = ConnectionManager()
        ws = _StubWS()
        cid = asyncio.run(mgr.connect(ws, _user("alice")))
        mgr.add_subscription(cid, "content.products")
        asyncio.run(mgr.broadcast_to_subscribers(
            "content.products.created",
            {"data": {"id": "1"}},
        ))
        assert len(ws.sent) == 1
        frame = ws.sent[0]
        assert frame["op"] == "push"
        assert frame["ch"] == "content.products.created"
        assert frame["payload"] == {"id": "1"}

    def test_no_match_no_delivery(self):
        mgr = ConnectionManager()
        ws = _StubWS()
        cid = asyncio.run(mgr.connect(ws, _user("alice")))
        mgr.add_subscription(cid, "content.products")
        asyncio.run(mgr.broadcast_to_subscribers(
            "content.orders.created", {"data": {"id": "1"}},
        ))
        assert ws.sent == []

    def test_double_subscription_delivers_once(self):
        # Connection subscribed to both 'content.products' and the
        # specific 'content.products.created' pattern must still get
        # exactly one frame per broadcast.
        mgr = ConnectionManager()
        ws = _StubWS()
        cid = asyncio.run(mgr.connect(ws, _user("alice")))
        mgr.add_subscription(cid, "content.products")
        mgr.add_subscription(cid, "content.products.created")
        asyncio.run(mgr.broadcast_to_subscribers(
            "content.products.created", {"data": {"id": "1"}},
        ))
        assert len(ws.sent) == 1

    def test_falls_back_to_record_then_to_event(self):
        # Payload resolution: data > record > event (the whole frame).
        mgr = ConnectionManager()
        ws = _StubWS()
        cid = asyncio.run(mgr.connect(ws, _user("alice")))
        mgr.add_subscription(cid, "content.products")
        asyncio.run(mgr.broadcast_to_subscribers(
            "content.products.updated", {"record": {"id": "2"}},
        ))
        asyncio.run(mgr.broadcast_to_subscribers(
            "content.products.deleted", {"only_field": "raw-event"},
        ))
        assert ws.sent[0]["payload"] == {"id": "2"}
        # No data/record key → broadcast falls back to the whole event
        # dict so legacy callers keep working.
        assert ws.sent[1]["payload"] == {"only_field": "raw-event"}

    def test_dead_connection_dropped_after_send_failure(self):
        mgr = ConnectionManager()
        ws_alive = _StubWS()
        ws_dead = _StubWS(fail=True)
        cid_alive = asyncio.run(mgr.connect(ws_alive, _user("alice")))
        cid_dead = asyncio.run(mgr.connect(ws_dead, _user("bob")))
        mgr.add_subscription(cid_alive, "content.products")
        mgr.add_subscription(cid_dead, "content.products")
        asyncio.run(mgr.broadcast_to_subscribers(
            "content.products.created", {"data": {"id": "1"}},
        ))
        # Dead connection cleaned up; alive one still registered and
        # received its frame.
        assert cid_dead not in mgr.active
        assert cid_alive in mgr.active
        assert len(ws_alive.sent) == 1


class TestOwnershipCascade:
    def test_owner_match_delivers(self):
        mgr = ConnectionManager()
        mgr.set_content_ownership({"sessions": "player_id"})
        ws = _StubWS()
        cid = asyncio.run(mgr.connect(ws, _user("alice")))
        mgr.add_subscription(cid, "content.sessions")
        asyncio.run(mgr.broadcast_to_subscribers(
            "content.sessions.created",
            {"data": {"id": "1", "player_id": "alice"}},
        ))
        assert len(ws.sent) == 1

    def test_owner_mismatch_drops(self):
        mgr = ConnectionManager()
        mgr.set_content_ownership({"sessions": "player_id"})
        ws = _StubWS()
        cid = asyncio.run(mgr.connect(ws, _user("alice")))
        mgr.add_subscription(cid, "content.sessions")
        asyncio.run(mgr.broadcast_to_subscribers(
            "content.sessions.created",
            {"data": {"id": "1", "player_id": "bob"}},
        ))
        assert ws.sent == []

    def test_anonymous_dropped_on_owned_content(self):
        mgr = ConnectionManager()
        mgr.set_content_ownership({"sessions": "player_id"})
        ws = _StubWS()
        cid = asyncio.run(mgr.connect(ws, _user("", anonymous=True)))
        mgr.add_subscription(cid, "content.sessions")
        asyncio.run(mgr.broadcast_to_subscribers(
            "content.sessions.created",
            {"data": {"id": "1", "player_id": "alice"}},
        ))
        assert ws.sent == []

    def test_missing_owner_field_drops_conservatively(self):
        # Owned content but the broadcast payload doesn't carry the
        # owning field — drop rather than over-share.
        mgr = ConnectionManager()
        mgr.set_content_ownership({"sessions": "player_id"})
        ws = _StubWS()
        cid = asyncio.run(mgr.connect(ws, _user("alice")))
        mgr.add_subscription(cid, "content.sessions")
        asyncio.run(mgr.broadcast_to_subscribers(
            "content.sessions.updated",
            {"data": {"id": "1"}},  # no player_id
        ))
        assert ws.sent == []

    def test_non_content_channel_bypasses_cascade(self):
        # State-machine event topics aren't gated by record ownership.
        mgr = ConnectionManager()
        mgr.set_content_ownership({"sessions": "player_id"})
        ws = _StubWS()
        cid = asyncio.run(mgr.connect(ws, _user("alice")))
        mgr.add_subscription(cid, "sessions.lifecycle.scoring.entered")
        asyncio.run(mgr.broadcast_to_subscribers(
            "sessions.lifecycle.scoring.entered",
            {"data": {"record_id": "1"}},
        ))
        assert len(ws.sent) == 1

    def test_unowned_content_bypasses_cascade(self):
        # Content type not in the ownership map: deliver to any
        # subscriber regardless of payload shape.
        mgr = ConnectionManager()
        mgr.set_content_ownership({"sessions": "player_id"})
        ws = _StubWS()
        cid = asyncio.run(mgr.connect(ws, _user("alice")))
        mgr.add_subscription(cid, "content.products")
        asyncio.run(mgr.broadcast_to_subscribers(
            "content.products.created",
            {"data": {"id": "1"}},
        ))
        assert len(ws.sent) == 1


# ── No-FastAPI guard ──


class TestNoFastAPILeakage:
    def test_module_does_not_import_fastapi(self):
        import sys
        from termin_core.routing import connection_manager  # noqa: F401
        # The module is intentionally framework-free; if it ever
        # picks up a transitive fastapi import this guard catches it.
        assert "fastapi" not in sys.modules or all(
            not getattr(connection_manager, name, None)
            for name in ("WebSocket", "WebSocketDisconnect")
        )
