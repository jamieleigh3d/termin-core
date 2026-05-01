# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the manual compute trigger handler added in slice 7.2.x."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from termin_core.errors import (
    TerminBadRequestError,
    TerminNotFoundError,
    TerminScopeError,
)
from termin_core.providers.identity_contract import (
    ANONYMOUS_PRINCIPAL,
    Principal,
)
from termin_core.routing import (
    AuthContext,
    TerminRequest,
    trigger_compute_handler,
)


def _principal(pid: str = "alice", *, scopes=()) -> AuthContext:
    return AuthContext(
        principal=Principal(id=pid, type="human", display_name=pid.title()),
        scopes=tuple(scopes),
        role_name="user",
    )


def _request(
    *,
    compute_name: str = "evaluator",
    body: Any = None,
    auth: AuthContext | None = None,
) -> TerminRequest:
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return TerminRequest(
        method="POST",
        path=f"/api/v1/compute/{compute_name}/trigger",
        path_params={"compute_name": compute_name},
        body=raw,
        auth=auth or _principal(scopes=()),
    )


class _StubCtx:
    """Minimal ctx for the trigger handler — exposes the four hooks
    the handler reads. ``executions`` records every execute_compute
    call so tests can assert routing."""

    def __init__(
        self,
        *,
        compute_lookup: dict[str, dict] | None = None,
        content_lookup: set[str] | None = None,
        check_compute_access: Any = None,
        terminator_routes: list | None = None,
    ) -> None:
        self.compute_lookup = compute_lookup or {}
        self.content_lookup = {name: {} for name in (content_lookup or set())}
        self.executions: list[tuple] = []
        if check_compute_access is not None:
            self.check_compute_access = check_compute_access
        if terminator_routes is not None:
            self.terminator = _StubTerminator(terminator_routes)

    async def execute_compute(self, comp, record, content_name, *,
                              main_loop, invoked_by=None):
        # v0.9.1 added the invoked_by kwarg so write_audit_trace can
        # stamp principal columns. The stub records it for assertions.
        self.executions.append((comp, record, content_name, main_loop, invoked_by))


class _StubTerminator:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    def route(self, err) -> None:
        self.sink.append(err)


def _comp(
    name: str,
    *,
    required_scope: str | None = None,
    input_content: list | None = None,
    provider: str = "ai-agent",
) -> dict:
    return {
        "name": {"snake": name, "display": name.title()},
        "required_scope": required_scope,
        "input_content": input_content or [],
        "provider": provider,
    }


# ── 404 / 403 paths ──


class TestNotFoundAndScopeGuards:
    def test_unknown_compute_404(self):
        ctx = _StubCtx()
        with pytest.raises(TerminNotFoundError):
            asyncio.run(trigger_compute_handler(_request(), ctx))

    def test_missing_required_scope_403(self):
        ctx = _StubCtx(compute_lookup={
            "evaluator": _comp("evaluator", required_scope="airlock.evaluate"),
        })
        with pytest.raises(TerminScopeError):
            asyncio.run(trigger_compute_handler(
                _request(auth=_principal(scopes=("other.scope",))), ctx,
            ))

    def test_required_scope_present_passes(self):
        ctx = _StubCtx(compute_lookup={
            "evaluator": _comp("evaluator", required_scope="airlock.evaluate"),
        })
        resp = asyncio.run(trigger_compute_handler(
            _request(auth=_principal(scopes=("airlock.evaluate",))), ctx,
        ))
        assert resp.status_code == 200

    def test_no_scope_required_passes_for_anonymous(self):
        ctx = _StubCtx(compute_lookup={"evaluator": _comp("evaluator")})
        anon_auth = AuthContext(principal=ANONYMOUS_PRINCIPAL)
        resp = asyncio.run(trigger_compute_handler(
            _request(auth=anon_auth), ctx,
        ))
        assert resp.status_code == 200


# ── Confidentiality gate ──


class TestConfidentialityGate:
    def test_gate_rejection_routes_to_terminator_and_403s(self):
        sink: list = []

        def gate(comp, scopes):
            return "Missing audit scope"

        ctx = _StubCtx(
            compute_lookup={"evaluator": _comp("evaluator")},
            check_compute_access=gate,
            terminator_routes=sink,
        )
        with pytest.raises(TerminScopeError):
            asyncio.run(trigger_compute_handler(_request(), ctx))
        assert len(sink) == 1
        assert sink[0].kind == "confidentiality_gate_rejected"
        assert sink[0].source == "Evaluator"

    def test_gate_pass_does_not_emit_terminator_event(self):
        sink: list = []
        ctx = _StubCtx(
            compute_lookup={"evaluator": _comp("evaluator")},
            check_compute_access=lambda comp, scopes: None,
            terminator_routes=sink,
        )
        asyncio.run(trigger_compute_handler(_request(), ctx))
        assert sink == []

    def test_no_check_compute_access_means_permissive(self):
        ctx = _StubCtx(compute_lookup={"evaluator": _comp("evaluator")})
        resp = asyncio.run(trigger_compute_handler(_request(), ctx))
        assert resp.status_code == 200


# ── Body parsing + content_name resolution ──


class TestContentNameResolution:
    def test_content_name_supplied_in_body(self):
        ctx = _StubCtx(
            compute_lookup={"evaluator": _comp("evaluator")},
            content_lookup={"sessions"},
        )
        asyncio.run(trigger_compute_handler(
            _request(body={"record": {"id": "r1"}, "content_name": "sessions"}),
            ctx,
        ))
        assert ctx.executions[0][2] == "sessions"

    def test_content_name_inferred_from_single_input(self):
        ctx = _StubCtx(
            compute_lookup={"evaluator": _comp(
                "evaluator", input_content=["sessions"])},
            content_lookup={"sessions"},
        )
        asyncio.run(trigger_compute_handler(
            _request(body={"record": {"id": "r1"}}), ctx,
        ))
        assert ctx.executions[0][2] == "sessions"

    def test_no_input_content_means_empty_string(self):
        ctx = _StubCtx(compute_lookup={"evaluator": _comp("evaluator")})
        asyncio.run(trigger_compute_handler(
            _request(body={"record": {}}), ctx,
        ))
        assert ctx.executions[0][2] == ""

    def test_multiple_input_content_without_name_400s(self):
        ctx = _StubCtx(compute_lookup={"evaluator": _comp(
            "evaluator", input_content=["sessions", "messages"])})
        with pytest.raises(TerminBadRequestError):
            asyncio.run(trigger_compute_handler(
                _request(body={"record": {}}), ctx,
            ))

    def test_unknown_content_name_400s(self):
        ctx = _StubCtx(
            compute_lookup={"evaluator": _comp("evaluator")},
            content_lookup={"sessions"},
        )
        with pytest.raises(TerminBadRequestError):
            asyncio.run(trigger_compute_handler(
                _request(body={"content_name": "ghost"}), ctx,
            ))

    def test_invalid_json_body_400s(self):
        ctx = _StubCtx(compute_lookup={"evaluator": _comp("evaluator")})
        req = TerminRequest(
            method="POST",
            path="/api/v1/compute/evaluator/trigger",
            path_params={"compute_name": "evaluator"},
            body=b"not json",
            auth=_principal(),
        )
        with pytest.raises(TerminBadRequestError):
            asyncio.run(trigger_compute_handler(req, ctx))

    def test_empty_body_treated_as_empty_dict(self):
        ctx = _StubCtx(compute_lookup={"evaluator": _comp("evaluator")})
        resp = asyncio.run(trigger_compute_handler(_request(body=None), ctx))
        assert resp.status_code == 200
        assert ctx.executions[0][1] == {}  # record defaulted to {}


# ── Successful response shape ──


class TestSuccessResponseShape:
    def test_response_carries_invocation_metadata(self):
        ctx = _StubCtx(compute_lookup={"evaluator": _comp(
            "evaluator", provider="ai-agent")})
        resp = asyncio.run(trigger_compute_handler(_request(), ctx))
        assert resp.status_code == 200
        body = resp.json_body
        assert body["compute"] == "Evaluator"
        assert body["provider"] == "ai-agent"
        assert body["trigger"] == "manual"
        assert body["status"] == "completed"
        assert isinstance(body["invocation_id"], str)
        assert len(body["invocation_id"]) > 0

    def test_execute_compute_invoked_with_record_and_content(self):
        ctx = _StubCtx(
            compute_lookup={"evaluator": _comp("evaluator")},
            content_lookup={"sessions"},
        )
        record = {"id": "r1", "name": "test"}
        asyncio.run(trigger_compute_handler(
            _request(body={"record": record, "content_name": "sessions"}),
            ctx,
        ))
        comp, rec, cname, _ml, _invoked_by = ctx.executions[0]
        assert comp["name"]["snake"] == "evaluator"
        assert rec == record
        assert cname == "sessions"
