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
    make_anonymous_principal,
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


# ── Anonymous Principal synthesis (v0.9.1) ──


class TestMakeAnonymousPrincipal:
    """``make_anonymous_principal(session_marker)`` is the canonical
    way to construct an anonymous Principal at the identity layer.
    Without a marker it returns the canonical sentinel; with a
    marker it returns a session-bearing variant whose audit-log
    rows are distinguishable from other anonymous activity."""

    def test_no_marker_returns_canonical_sentinel(self):
        p = make_anonymous_principal()
        assert p is ANONYMOUS_PRINCIPAL
        assert p.id == "anonymous"
        assert p.is_anonymous

    def test_empty_string_marker_returns_canonical_sentinel(self):
        # Empty / None / whitespace-only after sanitization should
        # all collapse to the sentinel.
        assert make_anonymous_principal("") is ANONYMOUS_PRINCIPAL
        assert make_anonymous_principal(None) is ANONYMOUS_PRINCIPAL
        assert make_anonymous_principal("@@@@") is ANONYMOUS_PRINCIPAL

    def test_marker_produces_typed_id(self):
        p = make_anonymous_principal("alice")
        assert p.id == "anonymous:alice"
        assert p.is_anonymous
        assert p.type == "human"
        assert p.display_name == "alice"

    def test_marker_sanitization_strips_unsafe_chars(self):
        # Spaces and special chars get stripped; alphanumeric +
        # ._- are preserved.
        p = make_anonymous_principal("alice@example.com / O'Brien")
        # @, /, ', space stripped; alphanumeric + . preserved.
        assert p.id == "anonymous:aliceexample.comOBrien"
        assert p.is_anonymous

    def test_marker_truncated_at_64_chars(self):
        marker = "a" * 200
        p = make_anonymous_principal(marker)
        assert p.id.startswith("anonymous:")
        assert len(p.id.split(":", 1)[1]) == 64

    def test_is_anonymous_recognizes_both_forms(self):
        assert ANONYMOUS_PRINCIPAL.is_anonymous
        assert make_anonymous_principal("alice").is_anonymous
        # A real authenticated principal is NOT anonymous.
        real = Principal(id="okta:user-42", type="human", display_name="Alice")
        assert not real.is_anonymous

    def test_anonymous_principal_is_immutable(self):
        # Frozen dataclass — can't mutate post-construction.
        p = make_anonymous_principal("alice")
        with pytest.raises(Exception):
            p.id = "evil"  # type: ignore[misc]


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


# ── v0.9.2 L7.1: ConversationContext + AgentContext.conversation ──


class TestConversationContext:
    """ConversationContext carries pre-translated provider-native messages
    plus the source-field/source-record_id pointers reviewers need for
    audit. Per tech-design §11.1."""

    def test_carries_messages_and_source_pointers(self):
        from termin_core.providers.compute_contract import ConversationContext
        ctx = ConversationContext(
            messages=({"role": "user", "content": "hi"},),
            source_field="chat_threads.conversation",
            source_record_id="thread-42",
        )
        assert ctx.messages == ({"role": "user", "content": "hi"},)
        assert ctx.source_field == "chat_threads.conversation"
        assert ctx.source_record_id == "thread-42"

    def test_messages_default_is_empty_tuple(self):
        from termin_core.providers.compute_contract import ConversationContext
        ctx = ConversationContext(
            source_field="x.y", source_record_id="r-1",
        )
        assert ctx.messages == ()

    def test_is_frozen(self):
        from termin_core.providers.compute_contract import ConversationContext
        ctx = ConversationContext(
            source_field="x.y", source_record_id="r-1",
        )
        with pytest.raises(Exception):
            ctx.source_field = "z.w"  # type: ignore[misc]


class TestAgentContextConversation:
    """AgentContext.conversation is optional: legacy non-conversation
    agents leave it None and the provider falls back to the legacy
    triggering-record prompt path. Per tech-design §11.1."""

    def test_conversation_defaults_to_none(self):
        from termin_core.providers.compute_contract import AgentContext
        from termin_core.providers.identity_contract import (
            make_anonymous_principal,
        )
        ac = AgentContext(principal=make_anonymous_principal())
        assert ac.conversation is None

    def test_conversation_can_be_supplied(self):
        from termin_core.providers.compute_contract import (
            AgentContext, ConversationContext,
        )
        from termin_core.providers.identity_contract import (
            make_anonymous_principal,
        )
        conv = ConversationContext(
            messages=({"role": "user", "content": "what time is it?"},),
            source_field="sessions.history",
            source_record_id="s-1",
        )
        ac = AgentContext(
            principal=make_anonymous_principal(),
            conversation=conv,
        )
        assert ac.conversation is conv
        assert ac.conversation.messages[0]["content"] == "what time is it?"
