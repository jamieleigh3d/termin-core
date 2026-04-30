# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the channel HTTP handlers added in slice 7.2.x.

invoke_channel_action_handler / channel_send_handler /
webhook_receive_handler each replace a FastAPI route in the runtime.
The dispatcher exception types (ChannelScopeError /
ChannelValidationError / ChannelError) are framework-agnostic shapes
the runtime defines next to its dispatcher; the handlers identify
them by class name, so the tests fake them with plain classes that
share the names.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from termin_core.errors import (
    TerminBadRequestError,
    TerminConflictError,
    TerminNotFoundError,
    TerminScopeError,
    TerminValidationError,
)
from termin_core.providers.identity_contract import (
    ANONYMOUS_PRINCIPAL,
    Principal,
)
from termin_core.routing import (
    AuthContext,
    TerminRequest,
    channel_send_handler,
    invoke_channel_action_handler,
    webhook_receive_handler,
)


# ── Fake dispatcher exception shapes ──


class ChannelScopeError(Exception):
    pass


class ChannelValidationError(Exception):
    pass


class ChannelError(Exception):
    pass


# ── Fixtures ──


def _principal(scopes=()) -> AuthContext:
    return AuthContext(
        principal=Principal(id="alice", type="human", display_name="Alice"),
        scopes=tuple(scopes),
        role_name="user",
    )


class _StubDispatcher:
    def __init__(self, *, specs=None, actions=None,
                 invoke=None, send=None) -> None:
        self._specs = specs or {}
        self._actions = actions or {}
        self._invoke = invoke
        self._send = send
        self._metrics: dict[str, dict] = {}

    def get_spec(self, name):
        return self._specs.get(name)

    def get_action_spec(self, ch, action):
        return self._actions.get((ch, action))

    async def channel_invoke(self, name, action, body, *, user_scopes):
        if self._invoke is None:
            return {"ok": True, "channel": name, "action": action}
        return await self._invoke(name, action, body, user_scopes)

    async def channel_send(self, name, body, *, user_scopes):
        if self._send is None:
            return {"ok": True, "channel": name}
        return await self._send(name, body, user_scopes)


class _StubStorage:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []

    async def create(self, content_name, record):
        self.created.append((content_name, record))
        return {"id": "rec-1", **record}


def _ctx(*, dispatcher=None, content_lookup=None, storage=None,
         run_event_handlers=None, publish=None) -> Any:
    class _Ctx:
        pass
    c = _Ctx()
    c.channel_dispatcher = dispatcher or _StubDispatcher()
    c.content_lookup = content_lookup or {}
    if storage is not None:
        c.storage = storage
    if run_event_handlers is not None:
        c.run_event_handlers_for_content = run_event_handlers
    if publish is not None:
        c.publish_content_event = publish
    return c


def _request(
    *,
    path_params: dict | None = None,
    body: Any = None,
    auth: AuthContext | None = None,
) -> TerminRequest:
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return TerminRequest(
        method="POST",
        path="/api/v1/channels/x",
        path_params=path_params or {},
        body=raw,
        auth=auth or _principal(),
    )


# ── invoke_channel_action_handler ──


class TestInvokeChannelAction:
    def test_unknown_channel_404s(self):
        ctx = _ctx()
        with pytest.raises(TerminNotFoundError):
            asyncio.run(invoke_channel_action_handler(
                _request(path_params={"channel_name": "ghost", "action_name": "alert"}), ctx,
            ))

    def test_unknown_action_404s(self):
        ctx = _ctx(dispatcher=_StubDispatcher(specs={"slack": {"name": "slack"}}))
        with pytest.raises(TerminNotFoundError):
            asyncio.run(invoke_channel_action_handler(
                _request(path_params={"channel_name": "slack", "action_name": "ghost"}), ctx,
            ))

    def test_dispatcher_scope_error_maps_to_403(self):
        async def boom(*args, **kwargs):
            raise ChannelScopeError("scope foo missing")
        ctx = _ctx(dispatcher=_StubDispatcher(
            specs={"slack": {"name": "slack"}}, actions={("slack", "alert"): {"name": "alert"}}, invoke=boom,
        ))
        with pytest.raises(TerminScopeError):
            asyncio.run(invoke_channel_action_handler(
                _request(path_params={"channel_name": "slack", "action_name": "alert"}), ctx,
            ))

    def test_dispatcher_validation_error_maps_to_422(self):
        async def boom(*args, **kwargs):
            raise ChannelValidationError("bad payload")
        ctx = _ctx(dispatcher=_StubDispatcher(
            specs={"slack": {"name": "slack"}}, actions={("slack", "alert"): {"name": "alert"}}, invoke=boom,
        ))
        with pytest.raises(TerminValidationError):
            asyncio.run(invoke_channel_action_handler(
                _request(path_params={"channel_name": "slack", "action_name": "alert"}), ctx,
            ))

    def test_dispatcher_provider_error_maps_to_409(self):
        async def boom(*args, **kwargs):
            raise ChannelError("upstream went away")
        ctx = _ctx(dispatcher=_StubDispatcher(
            specs={"slack": {"name": "slack"}}, actions={("slack", "alert"): {"name": "alert"}}, invoke=boom,
        ))
        with pytest.raises(TerminConflictError):
            asyncio.run(invoke_channel_action_handler(
                _request(path_params={"channel_name": "slack", "action_name": "alert"}), ctx,
            ))

    def test_success_returns_dispatcher_result(self):
        async def ok(*args, **kwargs):
            return {"sent": True, "id": "msg-1"}
        ctx = _ctx(dispatcher=_StubDispatcher(
            specs={"slack": {"name": "slack"}}, actions={("slack", "alert"): {"name": "alert"}}, invoke=ok,
        ))
        resp = asyncio.run(invoke_channel_action_handler(
            _request(path_params={"channel_name": "slack", "action_name": "alert"},
                     body={"text": "hi"}), ctx,
        ))
        assert resp.status_code == 200
        assert resp.json_body == {"sent": True, "id": "msg-1"}


# ── channel_send_handler ──


class TestChannelSend:
    def test_unknown_channel_404s(self):
        ctx = _ctx()
        with pytest.raises(TerminNotFoundError):
            asyncio.run(channel_send_handler(
                _request(path_params={"channel_name": "ghost"}), ctx,
            ))

    def test_dispatcher_scope_error_maps_to_403(self):
        async def boom(*args, **kwargs):
            raise ChannelScopeError("scope missing")
        ctx = _ctx(dispatcher=_StubDispatcher(
            specs={"slack": {"name": "slack"}}, send=boom,
        ))
        with pytest.raises(TerminScopeError):
            asyncio.run(channel_send_handler(
                _request(path_params={"channel_name": "slack"}), ctx,
            ))

    def test_dispatcher_provider_error_maps_to_409(self):
        async def boom(*args, **kwargs):
            raise ChannelError("upstream gone")
        ctx = _ctx(dispatcher=_StubDispatcher(
            specs={"slack": {"name": "slack"}}, send=boom,
        ))
        with pytest.raises(TerminConflictError):
            asyncio.run(channel_send_handler(
                _request(path_params={"channel_name": "slack"}), ctx,
            ))

    def test_success_returns_dispatcher_result(self):
        async def ok(*args, **kwargs):
            return {"id": "msg-99"}
        ctx = _ctx(dispatcher=_StubDispatcher(
            specs={"slack": {"name": "slack"}}, send=ok,
        ))
        resp = asyncio.run(channel_send_handler(
            _request(path_params={"channel_name": "slack"}, body={"text": "hi"}),
            ctx,
        ))
        assert resp.status_code == 200
        assert resp.json_body == {"id": "msg-99"}


# ── webhook_receive_handler ──


def _channel_spec(
    *,
    display="support inbound",
    snake="support_inbound",
    carries="tickets",
    requirements=(),
) -> dict:
    return {
        "name": {"snake": snake, "display": display},
        "carries_content": carries,
        "requirements": list(requirements),
    }


def _ticket_schema() -> dict:
    return {
        "fields": [
            {"name": {"snake": "title"}},
            {"name": {"snake": "description"}},
            {"name": "priority"},  # bare-string field name still works
        ],
    }


class TestWebhookReceive:
    def test_carries_no_content_400s(self):
        ctx = _ctx()
        with pytest.raises(TerminBadRequestError):
            asyncio.run(webhook_receive_handler(
                _request(body={"title": "x"}),
                ctx,
                channel_spec=_channel_spec(carries=""),
            ))

    def test_missing_send_scope_403s(self):
        ctx = _ctx(content_lookup={"tickets": _ticket_schema()})
        spec = _channel_spec(requirements=[
            {"direction": "send", "scope": "tickets.write"},
        ])
        with pytest.raises(TerminScopeError):
            asyncio.run(webhook_receive_handler(
                _request(body={"title": "x"}, auth=_principal()),
                ctx, channel_spec=spec,
            ))

    def test_send_scope_present_passes(self):
        storage = _StubStorage()
        ctx = _ctx(
            content_lookup={"tickets": _ticket_schema()},
            storage=storage,
        )
        spec = _channel_spec(requirements=[
            {"direction": "send", "scope": "tickets.write"},
        ])
        resp = asyncio.run(webhook_receive_handler(
            _request(body={"title": "x"},
                     auth=_principal(scopes=("tickets.write",))),
            ctx, channel_spec=spec,
        ))
        assert resp.status_code == 200

    def test_recv_scope_does_not_block_send(self):
        # Only send-direction requirements are checked at the webhook
        # boundary. A recv-direction scope on the channel is for
        # subscribers, not inbound senders.
        storage = _StubStorage()
        ctx = _ctx(
            content_lookup={"tickets": _ticket_schema()},
            storage=storage,
        )
        spec = _channel_spec(requirements=[
            {"direction": "recv", "scope": "tickets.read"},
        ])
        resp = asyncio.run(webhook_receive_handler(
            _request(body={"title": "x"}, auth=_principal()),
            ctx, channel_spec=spec,
        ))
        assert resp.status_code == 200

    def test_empty_body_400s(self):
        ctx = _ctx(content_lookup={"tickets": _ticket_schema()})
        spec = _channel_spec()
        req = TerminRequest(
            method="POST", path="/webhooks/support_inbound",
            body=b"", auth=_principal(),
        )
        with pytest.raises(TerminBadRequestError):
            asyncio.run(webhook_receive_handler(req, ctx, channel_spec=spec))

    def test_invalid_json_400s(self):
        ctx = _ctx(content_lookup={"tickets": _ticket_schema()})
        req = TerminRequest(
            method="POST", path="/webhooks/support_inbound",
            body=b"not json", auth=_principal(),
        )
        with pytest.raises(TerminBadRequestError):
            asyncio.run(webhook_receive_handler(
                req, ctx, channel_spec=_channel_spec(),
            ))

    def test_unknown_content_400s(self):
        ctx = _ctx(content_lookup={})
        with pytest.raises(TerminBadRequestError):
            asyncio.run(webhook_receive_handler(
                _request(body={"title": "x"}),
                ctx, channel_spec=_channel_spec(),
            ))

    def test_no_recognized_fields_422s(self):
        ctx = _ctx(
            content_lookup={"tickets": _ticket_schema()},
            storage=_StubStorage(),
        )
        with pytest.raises(TerminValidationError):
            asyncio.run(webhook_receive_handler(
                _request(body={"foo": "bar", "baz": "qux"}),
                ctx, channel_spec=_channel_spec(),
            ))

    def test_extra_fields_silently_dropped(self):
        storage = _StubStorage()
        ctx = _ctx(
            content_lookup={"tickets": _ticket_schema()},
            storage=storage,
        )
        asyncio.run(webhook_receive_handler(
            _request(body={"title": "x", "unknown": "drop me"}),
            ctx, channel_spec=_channel_spec(),
        ))
        # Only the known column made it to storage
        _, record = storage.created[0]
        assert record == {"title": "x"}

    def test_persists_record_and_publishes_created(self):
        storage = _StubStorage()
        events: list = []
        handler_calls: list = []

        async def handler(content_name, kind, record):
            handler_calls.append((content_name, kind, record["id"]))

        async def publish(kind, content_name, record):
            events.append((kind, content_name, record["id"]))

        ctx = _ctx(
            content_lookup={"tickets": _ticket_schema()},
            storage=storage,
            run_event_handlers=handler,
            publish=publish,
        )
        resp = asyncio.run(webhook_receive_handler(
            _request(body={"title": "Server down", "priority": "high"}),
            ctx, channel_spec=_channel_spec(),
        ))
        assert resp.status_code == 200
        assert resp.json_body == {
            "ok": True, "id": "rec-1", "channel": "support inbound",
        }
        assert storage.created == [
            ("tickets", {"title": "Server down", "priority": "high"}),
        ]
        assert handler_calls == [("tickets", "created", "rec-1")]
        assert events == [("created", "tickets", "rec-1")]

    def test_dispatcher_metric_increments_on_success(self):
        storage = _StubStorage()
        ctx = _ctx(
            content_lookup={"tickets": _ticket_schema()},
            storage=storage,
        )
        spec = _channel_spec()
        # Two successful inbounds -> received counter should hit 2.
        for _ in range(2):
            asyncio.run(webhook_receive_handler(
                _request(body={"title": "x"}), ctx, channel_spec=spec,
            ))
        bucket = ctx.channel_dispatcher._metrics.get("support inbound", {})
        assert bucket.get("received") == 2

    def test_handlers_optional(self):
        # run_event_handlers_for_content + publish_content_event are
        # optional ctx hooks; absence should not crash the handler.
        storage = _StubStorage()
        ctx = _ctx(
            content_lookup={"tickets": _ticket_schema()},
            storage=storage,
        )
        resp = asyncio.run(webhook_receive_handler(
            _request(body={"title": "x"}), ctx, channel_spec=_channel_spec(),
        ))
        assert resp.status_code == 200
