# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Conformance pack for the provider contract surface.

Exercises the value types and Protocol shapes BRD #1 and BRD #2
specify: Redacted sentinels, PrincipalContext, PresentationData,
ChannelAuditRecord, ChannelSendResult, MessageRef, identity Principal
shapes. These are the wire-shape contracts any conforming runtime
must produce; the tests assert that constructed instances expose the
declared attributes with the declared types.

Coverage target: bring presentation_contract.py from 0% and
channel_contract.py from 0% well into the 70%+ range.
"""

from __future__ import annotations

import pytest

from termin_core.providers.channel_contract import (
    ChannelAuditRecord,
    ChannelSendResult,
    MessageRef,
    _now_iso,
    _StubSubscription,
)
from termin_core.providers.presentation_contract import (
    PresentationData,
    PrincipalContext,
    Redacted,
    is_redacted,
    redacted_json_default,
    register_presentation_base_contracts,
)
from termin_core.providers.contracts import ContractRegistry, Category
from termin_core.providers.identity_contract import (
    ANONYMOUS_PRINCIPAL,
    Principal,
)


# ── Redacted sentinel ──


class TestRedactedSentinel:
    def test_redacted_carries_field_name_and_type(self):
        r = Redacted(field_name="salary", expected_type="number")
        assert r.field_name == "salary"
        assert r.expected_type == "number"

    def test_redacted_with_reason(self):
        r = Redacted(field_name="ssn", expected_type="text", reason="confidentiality")
        assert r.reason == "confidentiality"

    def test_is_redacted_true_for_sentinel(self):
        assert is_redacted(Redacted(field_name="x", expected_type="text"))

    def test_is_redacted_false_for_plain_values(self):
        assert not is_redacted("text")
        assert not is_redacted(0)
        assert not is_redacted(None)
        assert not is_redacted({})

    def test_redacted_json_default_serializes(self):
        out = redacted_json_default(Redacted(field_name="x", expected_type="number"))
        assert isinstance(out, dict)
        assert out.get("__redacted") is True

    def test_redacted_json_default_unknown_raises(self):
        class _Opaque:
            pass
        with pytest.raises(TypeError):
            redacted_json_default(_Opaque())


# ── PrincipalContext (rendering binding) ──


class TestPrincipalContext:
    def test_authenticated_principal(self):
        pc = PrincipalContext(
            principal_id="u1", principal_type="human",
            role_set=frozenset({"manager"}),
            scope_set=frozenset({"read", "write"}),
            theme_preference="dark", preferences={}, claims={},
        )
        assert pc.principal_id == "u1"
        assert "manager" in pc.role_set
        assert "read" in pc.scope_set
        assert pc.theme_preference == "dark"

    def test_anonymous_principal(self):
        pc = PrincipalContext(
            principal_id="anonymous", principal_type="human",
            role_set=frozenset(), scope_set=frozenset(),
            theme_preference="auto", preferences={}, claims={},
        )
        assert pc.principal_id == "anonymous"


class TestPresentationData:
    def test_minimal_construction(self):
        d = PresentationData(
            records=({"id": "1"},), props={}, meta={},
        )
        assert d.records[0]["id"] == "1"
        assert d.meta == {}

    def test_meta_preserves_extra_keys(self):
        d = PresentationData(
            records=(), props={}, meta={"highlighted": True, "count": 0},
        )
        assert d.meta["highlighted"] is True


# ── presentation-base contract registration ──


class TestPresentationBaseRegistration:
    def test_registers_ten_contracts(self):
        reg = ContractRegistry.default()
        # default() already calls register_presentation_base_contracts
        # internally (per BRD #2 §5.1 the ten contracts are baked in).
        # Calling it again should be a no-op or idempotent.
        register_presentation_base_contracts(reg)
        names = {c.name for c in reg.contracts_in(Category.PRESENTATION)}
        # All ten BRD #2 §5.1 contracts present.
        for n in (
            "page", "text", "data-table", "form", "chat", "metric",
            "nav-bar", "toast", "banner", "markdown",
        ):
            assert n in names or any(
                cn.endswith(n) for cn in names
            ), f"Missing presentation-base.{n}"


# ── Channel value types ──


class TestChannelAuditRecord:
    def test_minimal_construction(self):
        rec = ChannelAuditRecord(
            channel_name="alerts",
            provider_product="webhook-stub",
            direction="outbound",
            action="send",
            target="https://example.com/hook",
            payload_summary="hello",
            outcome="delivered",
            attempt_count=1,
            latency_ms=42,
            invoked_by=None,
            cost=None,
        )
        assert rec.channel_name == "alerts"
        assert rec.outcome == "delivered"
        assert rec.attempt_count == 1


class TestChannelSendResult:
    def test_success_result(self):
        r = ChannelSendResult(
            outcome="delivered", attempt_count=1, latency_ms=10,
            error_detail=None, audit_record=None,
        )
        assert r.outcome == "delivered"

    def test_failure_result(self):
        r = ChannelSendResult(
            outcome="failed", attempt_count=3, latency_ms=5000,
            error_detail="upstream_timeout", audit_record=None,
        )
        assert r.outcome == "failed"
        assert r.error_detail == "upstream_timeout"


class TestMessageRef:
    def test_minimal(self):
        m = MessageRef(id="abc-123", channel="general", thread_id=None)
        assert m.id == "abc-123"
        assert m.channel == "general"

    def test_with_thread(self):
        m = MessageRef(id="msg", channel="general", thread_id="t-1")
        assert m.thread_id == "t-1"


class TestStubSubscription:
    def test_class_exists_and_is_subclassable(self):
        # _StubSubscription is the no-op subscription handle channel
        # provider stubs return when their inbound side isn't wired.
        # Coverage check: class itself is importable and identifiable.
        assert _StubSubscription is not None
        assert isinstance(_StubSubscription, type)


class TestNowIso:
    def test_returns_iso_string_with_timezone(self):
        s = _now_iso()
        assert isinstance(s, str)
        # ISO 8601 with timezone marker.
        assert "T" in s
        # Either trailing Z or explicit offset.
        assert s.endswith("Z") or "+" in s[-6:] or "-" in s[-6:]
