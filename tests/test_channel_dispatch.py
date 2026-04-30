# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the framework-agnostic WebSocket dispatch loop added in
slice 7.2.f.

Each test drives :func:`dispatch_websocket_session` against a
scripted :class:`_ScriptedWS` that yields one frame at a time and
collects everything sent. The loop terminates when the WS raises
:class:`StopIteration`-shaped end-of-script — same semantic as a
client disconnect from the dispatcher's point of view.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

import pytest

from termin_core.routing import (
    ConnectionManager,
    dispatch_websocket_session,
)


# ── Scripted WS + ctx fixtures ──


class _ScriptedWS:
    """TerminWebSocket-shaped fake. Plays back a fixed sequence of
    incoming frames, then raises ``RuntimeError`` to end the loop —
    the dispatcher's contract is to let exceptions propagate so the
    adapter can clean up.
    """

    principal = None

    def __init__(self, frames: Iterable[dict]) -> None:
        self._frames = list(frames)
        self._idx = 0
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:  # pragma: no cover
        return

    async def receive_json(self) -> Any:
        if self._idx >= len(self._frames):
            raise RuntimeError("client disconnected")
        frame = self._frames[self._idx]
        self._idx += 1
        return frame

    async def receive_text(self) -> str:  # pragma: no cover
        raise NotImplementedError

    async def close(self, code: int = 1000) -> None:  # pragma: no cover
        return


class _StubCtx:
    """Minimal runtime context for the dispatcher.

    Carries a ConnectionManager and a ``list_records_for_ws`` hook
    keyed by content name. Tests configure both per case.
    """

    def __init__(
        self,
        records: dict[str, list[dict]] | None = None,
        ownership: dict[str, str] | None = None,
        raise_on: set[str] | None = None,
    ) -> None:
        self.conn_manager = ConnectionManager()
        if ownership:
            self.conn_manager.set_content_ownership(ownership)
        self._records = records or {}
        self._raise_on = raise_on or set()

    async def list_records_for_ws(self, content_name: str) -> list[dict]:
        if content_name in self._raise_on:
            raise RuntimeError(f"storage error for {content_name}")
        return list(self._records.get(content_name, []))


def _user(pid: str = "alice", *, role: str = "user", scopes=None, anonymous=False) -> dict:
    return {
        "the_user": {"id": pid, "is_anonymous": anonymous},
        "scopes": list(scopes or []),
        "role": role,
        "profile": {"id": pid, "display_name": pid.title()},
    }


def _run_session(ws: _ScriptedWS, ctx: _StubCtx, user: dict) -> None:
    """Drive the dispatcher until it raises (end of script)."""
    async def go():
        with pytest.raises(RuntimeError):
            await dispatch_websocket_session(ws, ctx, user)
    asyncio.run(go())


# ── Identity push frame ──


class TestIdentityFrame:
    def test_first_frame_carries_identity(self):
        ws = _ScriptedWS(frames=[])
        ctx = _StubCtx()
        _run_session(ws, ctx, _user("alice", role="manager", scopes=["x"]))
        assert ws.sent[0]["ch"] == "runtime.identity"
        assert ws.sent[0]["op"] == "push"
        assert ws.sent[0]["payload"]["role"] == "manager"
        assert ws.sent[0]["payload"]["scopes"] == ["x"]

    def test_anonymous_user_dict_still_produces_frame(self):
        ws = _ScriptedWS(frames=[])
        ctx = _StubCtx()
        _run_session(ws, ctx, _user("", anonymous=True, scopes=[]))
        assert ws.sent[0]["ch"] == "runtime.identity"

    def test_non_dict_user_falls_back_to_empty_identity(self):
        # A future identity-less adapter might pass None — ensure the
        # frame is still well-formed.
        ws = _ScriptedWS(frames=[])
        ctx = _StubCtx()
        _run_session(ws, ctx, None)  # type: ignore[arg-type]
        frame = ws.sent[0]
        assert frame["payload"] == {"role": "", "scopes": [], "profile": {}}


# ── subscribe op ──


class TestSubscribeOp:
    def test_subscribe_to_content_loads_initial_rows(self):
        ws = _ScriptedWS(frames=[
            {"op": "subscribe", "ch": "content.products", "ref": "r1"},
        ])
        ctx = _StubCtx(records={"products": [{"id": "1"}, {"id": "2"}]})
        _run_session(ws, ctx, _user())
        # One identity push + one subscribe response.
        assert len(ws.sent) == 2
        resp = ws.sent[1]
        assert resp["op"] == "response"
        assert resp["ref"] == "r1"
        assert resp["payload"]["current"] == [{"id": "1"}, {"id": "2"}]

    def test_subscribe_to_non_content_returns_empty_current(self):
        ws = _ScriptedWS(frames=[
            {"op": "subscribe", "ch": "events.custom", "ref": "r1"},
        ])
        ctx = _StubCtx()
        _run_session(ws, ctx, _user())
        resp = ws.sent[1]
        assert resp["op"] == "response"
        assert resp["payload"] == {"current": []}

    def test_subscribe_records_pattern_in_manager(self):
        ws = _ScriptedWS(frames=[
            {"op": "subscribe", "ch": "content.products", "ref": "r1"},
        ])
        ctx = _StubCtx(records={"products": []})
        _run_session(ws, ctx, _user("alice"))
        # Exactly one connection survived; its subscriptions include
        # the registered pattern.
        assert any(
            "content.products" in conn["subscriptions"]
            for conn in ctx.conn_manager.active.values()
        )

    def test_subscribe_storage_error_returns_error_frame(self):
        ws = _ScriptedWS(frames=[
            {"op": "subscribe", "ch": "content.products", "ref": "r1"},
        ])
        ctx = _StubCtx(raise_on={"products"})
        _run_session(ws, ctx, _user())
        resp = ws.sent[1]
        assert resp["op"] == "error"
        assert resp["ref"] == "r1"
        assert "storage error" in resp["payload"]["message"]

    def test_subscribe_applies_ownership_cascade_on_initial_load(self):
        ws = _ScriptedWS(frames=[
            {"op": "subscribe", "ch": "content.sessions", "ref": "r1"},
        ])
        ctx = _StubCtx(
            records={"sessions": [
                {"id": "1", "player_id": "alice"},
                {"id": "2", "player_id": "bob"},
            ]},
            ownership={"sessions": "player_id"},
        )
        _run_session(ws, ctx, _user("alice"))
        resp = ws.sent[1]
        assert resp["payload"]["current"] == [{"id": "1", "player_id": "alice"}]


# ── unsubscribe op ──


class TestUnsubscribeOp:
    def test_unsubscribe_drops_pattern_and_acks(self):
        ws = _ScriptedWS(frames=[
            {"op": "subscribe", "ch": "content.products", "ref": "r1"},
            {"op": "unsubscribe", "ch": "content.products", "ref": "r2"},
        ])
        ctx = _StubCtx(records={"products": []})
        _run_session(ws, ctx, _user())
        # 1 identity + 1 sub response + 1 unsub response = 3 frames
        assert len(ws.sent) == 3
        unsub = ws.sent[2]
        assert unsub["op"] == "response"
        assert unsub["ref"] == "r2"
        assert unsub["payload"] == {"unsubscribed": True}
        # Pattern actually removed from the manager.
        for conn in ctx.conn_manager.active.values():
            assert "content.products" not in conn["subscriptions"]


# ── request op ──


class TestRequestOp:
    def test_request_returns_data_payload_for_content(self):
        ws = _ScriptedWS(frames=[
            {"op": "request", "ch": "content.products", "ref": "r1"},
        ])
        ctx = _StubCtx(records={"products": [{"id": "1"}]})
        _run_session(ws, ctx, _user())
        resp = ws.sent[1]
        assert resp["op"] == "response"
        assert resp["payload"] == {"data": [{"id": "1"}]}

    def test_request_for_non_content_topic_is_silent(self):
        # The original handler only responded for content.* topics
        # on request — non-content topics get nothing.
        ws = _ScriptedWS(frames=[
            {"op": "request", "ch": "events.custom", "ref": "r1"},
        ])
        ctx = _StubCtx()
        _run_session(ws, ctx, _user())
        # Only the identity push; no response to the request frame.
        assert len(ws.sent) == 1
        assert ws.sent[0]["ch"] == "runtime.identity"

    def test_request_storage_error_returns_error_frame(self):
        ws = _ScriptedWS(frames=[
            {"op": "request", "ch": "content.products", "ref": "r1"},
        ])
        ctx = _StubCtx(raise_on={"products"})
        _run_session(ws, ctx, _user())
        resp = ws.sent[1]
        assert resp["op"] == "error"
        assert resp["ref"] == "r1"

    def test_request_applies_ownership_cascade(self):
        ws = _ScriptedWS(frames=[
            {"op": "request", "ch": "content.sessions", "ref": "r1"},
        ])
        ctx = _StubCtx(
            records={"sessions": [
                {"id": "1", "player_id": "alice"},
                {"id": "2", "player_id": "bob"},
            ]},
            ownership={"sessions": "player_id"},
        )
        _run_session(ws, ctx, _user("alice"))
        resp = ws.sent[1]
        assert resp["payload"]["data"] == [{"id": "1", "player_id": "alice"}]


# ── Multi-frame sessions exercise the loop ──


class TestMultiFrameSession:
    def test_subscribe_then_request_then_unsubscribe(self):
        ws = _ScriptedWS(frames=[
            {"op": "subscribe", "ch": "content.products", "ref": "r1"},
            {"op": "request", "ch": "content.products", "ref": "r2"},
            {"op": "unsubscribe", "ch": "content.products", "ref": "r3"},
        ])
        ctx = _StubCtx(records={"products": [{"id": "1"}]})
        _run_session(ws, ctx, _user())
        ops = [f.get("op") for f in ws.sent]
        # identity push, sub response, request response, unsub response
        assert ops == ["push", "response", "response", "response"]
        refs = [f.get("ref") for f in ws.sent]
        assert refs == [None, "r1", "r2", "r3"]
