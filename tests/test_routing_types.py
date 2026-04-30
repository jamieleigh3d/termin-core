# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the framework-agnostic routing value types added in
slice 7.2.d.

Tests focus on the contract adapters and handlers depend on:
case-insensitive header lookup, JSON body parsing, form parsing,
RouteSpec method normalization, and the TerminWebSocket Protocol
shape.
"""

import asyncio
import json
from typing import Any, Optional

import pytest

from termin_core.routing import (
    TerminRequest,
    TerminResponse,
    TerminWebSocket,
    RouteSpec,
    WebSocketRouteSpec,
    HttpHandler,
    WebSocketHandler,
)


# ── TerminRequest ──


class TestTerminRequestConstruction:
    def test_minimal_construction(self):
        req = TerminRequest(method="GET", path="/api/v1/products")
        assert req.method == "GET"
        assert req.path == "/api/v1/products"
        assert req.path_params == {}
        assert req.query_params == {}
        assert req.body == b""
        assert req.principal is None

    def test_method_is_uppercased(self):
        req = TerminRequest(method="get", path="/")
        assert req.method == "GET"
        req2 = TerminRequest(method="Patch", path="/")
        assert req2.method == "PATCH"

    def test_redirect_default_status(self):
        # not on TerminRequest — verified on TerminResponse below.
        pass


class TestTerminRequestHeaders:
    """Headers must be case-insensitive per RFC 7230 §3.2."""

    def test_case_insensitive_lookup(self):
        req = TerminRequest(
            method="GET", path="/",
            headers={"Content-Type": "application/json"},
        )
        assert req.headers["content-type"] == "application/json"
        assert req.headers["CONTENT-TYPE"] == "application/json"
        assert req.headers["Content-Type"] == "application/json"

    def test_case_insensitive_contains(self):
        req = TerminRequest(
            method="GET", path="/",
            headers={"Authorization": "Bearer xyz"},
        )
        assert "authorization" in req.headers
        assert "AUTHORIZATION" in req.headers
        assert "missing" not in req.headers

    def test_case_insensitive_get_with_default(self):
        req = TerminRequest(
            method="GET", path="/",
            headers={"X-Custom": "v"},
        )
        assert req.headers.get("x-custom") == "v"
        assert req.headers.get("missing", "default") == "default"

    def test_dict_passed_in_promoted_to_case_insensitive(self):
        """Adapters often build a plain dict; constructor promotes it."""
        plain = {"Accept": "text/html", "User-Agent": "test"}
        req = TerminRequest(method="GET", path="/", headers=plain)
        assert req.headers["accept"] == "text/html"
        assert req.headers["user-agent"] == "test"

    def test_set_then_overwrite_different_case(self):
        req = TerminRequest(method="GET", path="/")
        req.headers["Content-Type"] = "text/plain"
        # Overwriting with different case must not duplicate.
        req.headers["content-type"] = "application/json"
        assert req.headers["Content-Type"] == "application/json"
        assert len(req.headers) == 1

    def test_delete(self):
        req = TerminRequest(
            method="GET", path="/",
            headers={"X-Custom": "v"},
        )
        del req.headers["x-custom"]
        assert "x-custom" not in req.headers


class TestTerminRequestBodyParsing:
    """JSON and form bodies parse via async helpers."""

    @pytest.mark.asyncio
    async def test_json_parses_body(self):
        req = TerminRequest(
            method="POST", path="/",
            body=b'{"name": "widget", "qty": 5}',
        )
        data = await req.json()
        assert data == {"name": "widget", "qty": 5}

    @pytest.mark.asyncio
    async def test_json_empty_body_returns_none(self):
        req = TerminRequest(method="GET", path="/", body=b"")
        assert await req.json() is None

    @pytest.mark.asyncio
    async def test_json_invalid_raises(self):
        req = TerminRequest(method="POST", path="/", body=b"not json {")
        with pytest.raises(ValueError):
            await req.json()

    @pytest.mark.asyncio
    async def test_form_parses_url_encoded(self):
        req = TerminRequest(
            method="POST", path="/",
            body=b"name=widget&qty=5",
        )
        data = await req.form()
        assert data == {"name": "widget", "qty": "5"}

    @pytest.mark.asyncio
    async def test_form_empty_body_returns_empty_dict(self):
        req = TerminRequest(method="POST", path="/", body=b"")
        assert await req.form() == {}

    @pytest.mark.asyncio
    async def test_form_multi_keeps_repeats(self):
        req = TerminRequest(
            method="POST", path="/",
            body=b"tag=red&tag=blue&size=L",
        )
        data = await req.form_multi()
        assert data == {"tag": ["red", "blue"], "size": ["L"]}


# ── TerminResponse ──


class TestTerminResponse:
    def test_default_status_is_200(self):
        r = TerminResponse()
        assert r.status_code == 200
        assert r.body is None
        assert r.json_body is None

    def test_redirect_default_status_is_303(self):
        r = TerminResponse(redirect_url="/login")
        assert r.status_code == 303
        assert r.redirect_url == "/login"

    def test_redirect_explicit_status_preserved(self):
        r = TerminResponse(redirect_url="/", status_code=301)
        assert r.status_code == 301

    def test_headers_case_insensitive(self):
        r = TerminResponse(headers={"Content-Type": "application/json"})
        assert r.headers["content-type"] == "application/json"

    def test_can_carry_streaming_iterator(self):
        async def gen():
            yield b"chunk1"
            yield b"chunk2"

        r = TerminResponse(streaming=gen())
        assert r.streaming is not None


# ── RouteSpec ──


class TestRouteSpec:
    def test_method_uppercased(self):
        async def handler(req: TerminRequest, ctx: Any) -> TerminResponse:
            return TerminResponse()

        spec = RouteSpec(method="get", path="/", handler=handler)
        assert spec.method == "GET"

    def test_handler_is_async_callable(self):
        async def handler(req: TerminRequest, ctx: Any) -> TerminResponse:
            return TerminResponse(json_body={"ok": True})

        spec = RouteSpec(method="GET", path="/", handler=handler)
        result = asyncio.run(spec.handler(
            TerminRequest(method="GET", path="/"), None
        ))
        assert result.json_body == {"ok": True}

    def test_required_scope_optional(self):
        async def handler(req, ctx):
            return TerminResponse()

        spec = RouteSpec(method="GET", path="/", handler=handler)
        assert spec.required_scope is None

        spec_scoped = RouteSpec(
            method="POST", path="/", handler=handler,
            required_scope="orders.write",
        )
        assert spec_scoped.required_scope == "orders.write"

    def test_path_pattern_preserved_verbatim(self):
        """Adapter is responsible for translating to its framework's
        pattern syntax; RouteSpec stores the pattern verbatim."""
        async def handler(req, ctx):
            return TerminResponse()

        spec = RouteSpec(
            method="GET", path="/api/v1/{content}/{id}",
            handler=handler,
        )
        assert spec.path == "/api/v1/{content}/{id}"


class TestWebSocketRouteSpec:
    def test_minimal_construction(self):
        async def handler(ws: TerminWebSocket, ctx: Any) -> None:
            await ws.accept()

        spec = WebSocketRouteSpec(path="/runtime/ws", handler=handler)
        assert spec.path == "/runtime/ws"
        assert spec.required_scope is None


# ── TerminWebSocket Protocol ──


class TestTerminWebSocketProtocol:
    """The Protocol shape — instances satisfying these methods + the
    principal attribute pass an isinstance check (Protocol is
    runtime_checkable per the design)."""

    def test_runtime_checkable_recognizes_compliant_class(self):
        class _Stub:
            principal = None

            async def accept(self): pass
            async def send_json(self, data): pass
            async def send_bytes(self, data): pass
            async def receive_json(self): return None
            async def receive_text(self): return ""
            async def close(self, code=1000): pass

        ws = _Stub()
        assert isinstance(ws, TerminWebSocket)

    def test_runtime_checkable_rejects_missing_method(self):
        class _Incomplete:
            principal = None

            async def accept(self): pass
            # missing send_json, etc.

        ws = _Incomplete()
        assert not isinstance(ws, TerminWebSocket)


# ── Framework-free guard ──


class TestNoFrameworkLeak:
    """The framework-free guard from test_smoke.py covers the import
    graph; this targets the routing types specifically."""

    def test_no_fastapi_in_request_module(self):
        import termin_core.routing.request as m
        for name in dir(m):
            obj = getattr(m, name)
            mod = getattr(obj, "__module__", "")
            assert not mod.startswith("fastapi"), (
                f"{name} comes from {mod}; routing types must stay "
                f"framework-free.")
            assert not mod.startswith("starlette"), (
                f"{name} comes from {mod}; routing types must stay "
                f"framework-free.")

    def test_handlers_are_pure_callables(self):
        """RouteSpec.handler is a Callable[[TerminRequest, Any],
        Awaitable[TerminResponse]] — no framework decorator
        machinery in the type."""
        async def handler(req: TerminRequest, ctx: Any) -> TerminResponse:
            return TerminResponse()

        spec = RouteSpec(method="GET", path="/", handler=handler)
        # The handler is a plain coroutine function, not a wrapped
        # FastAPI route function with .__route__ attribute.
        assert callable(spec.handler)
        assert asyncio.iscoroutinefunction(spec.handler)
