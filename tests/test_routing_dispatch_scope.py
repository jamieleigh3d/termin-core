# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for scope enforcement inside ``dispatch_http_request``.

Closes (1) of `termin-core` issue #6 — an alt-runtime adopter
observed that ``dispatch_http_request`` was silently bypassing
``RouteSpec.required_scope``, so adapters routing requests through
it received zero per-route scope enforcement.

The contract these tests pin:

  * If a matched RouteSpec has ``required_scope is None``, the
    handler is invoked regardless of caller auth state.
  * If a matched RouteSpec has ``required_scope == "X"``, the
    dispatcher returns 403 unless ``request.auth`` is set AND
    ``request.auth.has_scope("X")`` is true.
  * Scope enforcement runs BEFORE the handler is called — the
    handler must not see traffic that fails the scope gate.

These tests construct a minimal ctx with a hand-built route table
(the IR-walking ``build_route_specs`` is exercised elsewhere); the
focus is the dispatcher's enforcement behavior, not route
discovery.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from termin_core.providers.identity_contract import (
    ANONYMOUS_PRINCIPAL,
    Principal,
)
from termin_core.routing import (
    RouteSpec,
    TerminRequest,
    TerminResponse,
)
from termin_core.routing.auth import AuthContext
from termin_core.routing.dispatch import dispatch_http_request


# ── Fixtures ──


def _make_ctx(specs: list[RouteSpec]) -> Any:
    """Build a minimal ctx that ``dispatch_http_request`` accepts.

    The dispatcher caches the spec list on ``ctx._route_specs_cache``
    on first call. We pre-seed it so the IR-walker isn't exercised.
    """
    from termin_core.routing.dispatch import _path_to_regex

    class _Ctx:
        pass

    ctx = _Ctx()
    regexes = [(spec, _path_to_regex(spec.path)) for spec in specs]
    ctx._route_specs_cache = (specs, regexes)
    return ctx


async def _ok_handler(request: TerminRequest, ctx: Any) -> TerminResponse:
    """Handler that records it was invoked and returns 200."""
    ctx._handler_was_called = True
    return TerminResponse(status_code=200, json_body={"ok": True})


def _make_auth(scopes: tuple[str, ...]) -> AuthContext:
    """Build an AuthContext with a real (non-anonymous) principal."""
    p = Principal(id="user-1", type="human", display_name="Test User")
    return AuthContext(principal=p, scopes=scopes, roles=("tester",))


def _make_anonymous_auth() -> AuthContext:
    """Build an AuthContext for the anonymous principal (no scopes)."""
    return AuthContext(principal=ANONYMOUS_PRINCIPAL, scopes=())


# ── Tests ──


class TestNoScopeRequired:
    """RouteSpec.required_scope is None — handler always invoked."""

    def test_no_auth_at_all(self):
        spec = RouteSpec(
            method="GET", path="/api/v1/public",
            handler=_ok_handler, required_scope=None,
        )
        ctx = _make_ctx([spec])
        req = TerminRequest(method="GET", path="/api/v1/public")
        resp = asyncio.run(dispatch_http_request(ctx, req))
        assert resp.status_code == 200
        assert getattr(ctx, "_handler_was_called", False) is True

    def test_authenticated_with_no_scopes(self):
        spec = RouteSpec(
            method="GET", path="/api/v1/public",
            handler=_ok_handler, required_scope=None,
        )
        ctx = _make_ctx([spec])
        req = TerminRequest(
            method="GET", path="/api/v1/public",
            auth=_make_auth(scopes=()),
        )
        resp = asyncio.run(dispatch_http_request(ctx, req))
        assert resp.status_code == 200


class TestScopeRequiredAndPresent:
    """RouteSpec.required_scope set, caller holds it — pass through."""

    def test_exact_scope_match(self):
        spec = RouteSpec(
            method="POST", path="/api/v1/orders",
            handler=_ok_handler, required_scope="write orders",
        )
        ctx = _make_ctx([spec])
        req = TerminRequest(
            method="POST", path="/api/v1/orders",
            auth=_make_auth(scopes=("write orders",)),
        )
        resp = asyncio.run(dispatch_http_request(ctx, req))
        assert resp.status_code == 200
        assert getattr(ctx, "_handler_was_called", False) is True

    def test_scope_present_among_others(self):
        """Caller holds the required scope + some extras."""
        spec = RouteSpec(
            method="POST", path="/api/v1/orders",
            handler=_ok_handler, required_scope="write orders",
        )
        ctx = _make_ctx([spec])
        req = TerminRequest(
            method="POST", path="/api/v1/orders",
            auth=_make_auth(
                scopes=("read orders", "write orders", "admin orders"),
            ),
        )
        resp = asyncio.run(dispatch_http_request(ctx, req))
        assert resp.status_code == 200


class TestScopeRequiredAndMissing:
    """RouteSpec.required_scope set, caller does NOT hold it — 403."""

    def test_caller_has_wrong_scopes(self):
        spec = RouteSpec(
            method="POST", path="/api/v1/orders",
            handler=_ok_handler, required_scope="write orders",
        )
        ctx = _make_ctx([spec])
        req = TerminRequest(
            method="POST", path="/api/v1/orders",
            auth=_make_auth(scopes=("read orders",)),
        )
        resp = asyncio.run(dispatch_http_request(ctx, req))
        assert resp.status_code == 403
        # The handler must NOT have been invoked.
        assert getattr(ctx, "_handler_was_called", False) is False

    def test_caller_has_no_scopes(self):
        spec = RouteSpec(
            method="POST", path="/api/v1/orders",
            handler=_ok_handler, required_scope="write orders",
        )
        ctx = _make_ctx([spec])
        req = TerminRequest(
            method="POST", path="/api/v1/orders",
            auth=_make_auth(scopes=()),
        )
        resp = asyncio.run(dispatch_http_request(ctx, req))
        assert resp.status_code == 403
        assert getattr(ctx, "_handler_was_called", False) is False

    def test_no_auth_on_request(self):
        """No auth at all and route requires a scope → 403."""
        spec = RouteSpec(
            method="POST", path="/api/v1/orders",
            handler=_ok_handler, required_scope="write orders",
        )
        ctx = _make_ctx([spec])
        req = TerminRequest(method="POST", path="/api/v1/orders")
        resp = asyncio.run(dispatch_http_request(ctx, req))
        assert resp.status_code == 403
        assert getattr(ctx, "_handler_was_called", False) is False

    def test_anonymous_principal_blocked(self):
        """Anonymous AuthContext (real principal, empty scopes) → 403."""
        spec = RouteSpec(
            method="POST", path="/api/v1/orders",
            handler=_ok_handler, required_scope="write orders",
        )
        ctx = _make_ctx([spec])
        req = TerminRequest(
            method="POST", path="/api/v1/orders",
            auth=_make_anonymous_auth(),
        )
        resp = asyncio.run(dispatch_http_request(ctx, req))
        assert resp.status_code == 403


class TestScopeErrorBody:
    """403 response body shape is stable (adapters render it
    further; the dispatcher just needs to produce a recognizable
    detail key)."""

    def test_403_carries_detail(self):
        spec = RouteSpec(
            method="GET", path="/x", handler=_ok_handler,
            required_scope="admin",
        )
        ctx = _make_ctx([spec])
        req = TerminRequest(method="GET", path="/x")
        resp = asyncio.run(dispatch_http_request(ctx, req))
        assert resp.status_code == 403
        assert "detail" in resp.json_body
        # Adapters may localize, but the substring "scope" or
        # "Forbidden" is the load-bearing signal the alt-runtime
        # adopter can pattern-match on.
        detail = resp.json_body["detail"].lower()
        assert "forbidden" in detail or "scope" in detail


class TestScopeAndPathParams:
    """Scope enforcement must happen AFTER path-param extraction so
    that adapters that want to log the resolved route get the right
    path_params. (It runs before the handler, but the path_params
    are still part of the matched request shape.)"""

    def test_404_takes_priority_over_scope(self):
        """No matching path → 404, not 403 (scope is only meaningful
        once a route matches)."""
        spec = RouteSpec(
            method="GET", path="/api/v1/orders",
            handler=_ok_handler, required_scope="read orders",
        )
        ctx = _make_ctx([spec])
        req = TerminRequest(method="GET", path="/api/v1/nope")
        resp = asyncio.run(dispatch_http_request(ctx, req))
        assert resp.status_code == 404

    def test_405_takes_priority_over_scope(self):
        """Method mismatch on matching path → 405, not 403."""
        spec = RouteSpec(
            method="POST", path="/api/v1/orders",
            handler=_ok_handler, required_scope="write orders",
        )
        ctx = _make_ctx([spec])
        req = TerminRequest(
            method="GET", path="/api/v1/orders",
            auth=_make_auth(scopes=()),
        )
        resp = asyncio.run(dispatch_http_request(ctx, req))
        assert resp.status_code == 405


class TestMultipleRoutes:
    """Sanity: per-spec scope checks don't leak across specs in the
    same route table."""

    def test_one_route_scoped_other_not(self):
        scoped = RouteSpec(
            method="GET", path="/admin",
            handler=_ok_handler, required_scope="admin",
        )
        public = RouteSpec(
            method="GET", path="/public",
            handler=_ok_handler, required_scope=None,
        )
        ctx = _make_ctx([scoped, public])

        # /public goes through without auth.
        req_pub = TerminRequest(method="GET", path="/public")
        resp_pub = asyncio.run(dispatch_http_request(ctx, req_pub))
        assert resp_pub.status_code == 200

        # /admin needs the scope.
        req_admin_anon = TerminRequest(method="GET", path="/admin")
        resp_admin_anon = asyncio.run(
            dispatch_http_request(ctx, req_admin_anon),
        )
        assert resp_admin_anon.status_code == 403

        # /admin with the right scope.
        req_admin_ok = TerminRequest(
            method="GET", path="/admin",
            auth=_make_auth(scopes=("admin",)),
        )
        resp_admin_ok = asyncio.run(
            dispatch_http_request(ctx, req_admin_ok),
        )
        assert resp_admin_ok.status_code == 200
