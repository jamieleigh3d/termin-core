# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Channel contract surfaces — v0.9 Phase 4.

Per BRD §6.4, the Channel category has four named contracts. Each has a
distinct operation signature, so each gets its own Protocol — following the
same pattern as compute_contract.py.

Contract → Protocol mapping:

    (channels, "webhook")      → WebhookChannelProvider
    (channels, "email")        → EmailChannelProvider
    (channels, "messaging")    → MessagingChannelProvider
    (channels, "event-stream") → EventStreamChannelProvider  (stub deferred)

Concrete providers (stub products) live in
`termin_runtime/providers/builtins/`. The ChannelDispatcher constructs a
provider per channel via the ProviderRegistry, looking up by the source's
`Provider is "<name>"` line and the deploy config's
`bindings.channels["<channel-name>"].provider`.

Data shapes declared here: ChannelSendResult, ChannelAuditRecord, MessageRef,
Subscription. Per BRD §6.4.5, every channel send and inbound message produces
a ChannelAuditRecord; the runtime logs it.

Behavioral requirements (BRD §6.4.5) the runtime enforces around providers:

  - **Failure mode default: log-and-drop.** Provider exceptions are caught by
    the ChannelDispatcher; the app keeps running. `surface-as-error` and
    `queue-and-retry-forever` are grammar placeholders in Phase 4 — always
    log-and-drop at runtime in this release.
  - **Per-action authorization** enforced by ChannelDispatcher before
    calling the provider. Providers receive only authorized calls.
  - **Idempotency keys** are runtime-generated; providers may use them
    where applicable. Phase 4 stubs ignore them.
  - **ProviderRecord.features** declares which action verbs a product
    implements. The runtime validates at startup that the IR's declared
    action vocabulary is a subset of the bound provider's features.
    (BRD §6.4.3: "Provider declares which actions it implements; host
    validates source against the declared subset.")
  - **Audit is mandatory.** Every ChannelSendResult should carry an
    audit_record. Stubs produce minimal records.

The event-stream contract (BRD §6.4.4) is defined here for completeness
but its stub implementation is deferred — no fixture requires it in Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, runtime_checkable


# ── Shared data shapes ──


@dataclass(frozen=True)
class ChannelAuditRecord:
    """Audit record produced by every channel send / inbound message.

    Per BRD §6.4.5. The runtime logs this; operators access it through
    the standard audit mechanism. Distinct from compute AuditRecord —
    channels are Tier 2 (BRD §4) and have a simpler reproducibility
    surface.

    Fields:
        channel_name: logical channel name from source.
        provider_product: the registered product name (e.g., "stub", "slack").
        direction: "outbound" | "inbound".
        action: verb used (e.g., "send", "post", "send_message").
        target: resolved physical target — URL, Slack channel name, etc.
            Never the logical source name; always the deploy-config-resolved
            target so operators can trace exactly where data went.
        payload_summary: truncated/redacted body. Providers should limit this
            to the first 200 characters of a serialized payload.
        outcome: "delivered" | "failed" | "queued".
        attempt_count: how many attempts were made (1 on first success).
        latency_ms: wall-clock time from first attempt to final outcome.
        invoked_by: principal id of the caller; None for inbound messages
            and system-triggered sends.
        cost: provider-reported cost if known (e.g., SMS per-message pricing).
            None when not applicable or not reported.
    """
    channel_name: str
    provider_product: str
    direction: str                     # "outbound" | "inbound"
    action: str                        # e.g., "send", "post", "send_message"
    target: str                        # resolved target
    payload_summary: str               # truncated/redacted body
    outcome: str                       # "delivered" | "failed" | "queued"
    attempt_count: int
    latency_ms: int
    invoked_by: Optional[str] = None   # principal id; None for inbound+system
    cost: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.outcome not in ("delivered", "failed", "queued"):
            raise ValueError(
                f"ChannelAuditRecord.outcome must be 'delivered' | 'failed' | "
                f"'queued', got {self.outcome!r}"
            )
        if self.direction not in ("outbound", "inbound"):
            raise ValueError(
                f"ChannelAuditRecord.direction must be 'outbound' | 'inbound', "
                f"got {self.direction!r}"
            )


@dataclass(frozen=True)
class ChannelSendResult:
    """Outcome of any channel send operation.

    Returned by WebhookChannelProvider.send(), EmailChannelProvider.send(),
    and MessagingChannelProvider.send(). MessagingChannelProvider.send()
    returns a MessageRef instead (which carries richer identity) — see below.

    The ChannelDispatcher catches exceptions from providers and converts them
    to a ChannelSendResult with outcome="failed" + error_detail populated,
    satisfying the log-and-drop default (BRD §6.4.5).
    """
    outcome: str                       # "delivered" | "failed" | "queued"
    attempt_count: int = 1
    latency_ms: int = 0
    error_detail: Optional[str] = None
    audit_record: Optional[ChannelAuditRecord] = None

    def __post_init__(self) -> None:
        if self.outcome not in ("delivered", "failed", "queued"):
            raise ValueError(
                f"ChannelSendResult.outcome must be 'delivered' | 'failed' | "
                f"'queued', got {self.outcome!r}"
            )


@dataclass(frozen=True)
class MessageRef:
    """A reference to a sent message in a messaging platform.

    Returned by MessagingChannelProvider.send(). Callers can pass
    message_ref.id to update() or react() to operate on the message.
    thread_id is the platform thread identifier if the message was sent
    in a thread context.
    """
    id: str                            # platform-internal message id
    channel: str                       # platform channel name/id
    thread_id: Optional[str] = None   # thread id if applicable


class _StubSubscription:
    """Internal sentinel for stub subscriptions. Not part of the Protocol."""
    def __init__(self, target: str, handler_list: list) -> None:
        self.target = target
        self._handlers = handler_list

    def cancel(self) -> None:
        """Remove all handlers for this subscription target."""
        self._handlers.clear()


# ── Protocol: webhook ──


@runtime_checkable
class WebhookChannelProvider(Protocol):
    """The webhook contract surface (BRD §6.4.1).

    Outbound HTTP POST. The destination URL, timeout, retry policy, and
    auth headers live in provider config — never in the call args (the
    leak-free principle, BRD §5.1). Source declares the channel logically;
    deploy config binds the physical endpoint.

    Action vocabulary in source: `Post <body>`.

    Provider config shape (bindings.channels.<name>.config):
        {
            "target": "https://hooks.example.com/path",
            "timeout_ms": 10000,
            "retry": {"max_attempts": 5, "backoff": "exponential"},
            "auth": {"type": "hmac", "secret_ref": "${HOOK_SECRET}"}
        }
    """

    async def send(
        self,
        body: Any,
        headers: Optional[Mapping[str, str]] = None,
    ) -> ChannelSendResult:
        """POST body to the configured target URL.

        Args:
            body: payload to send. The provider serializes to JSON or
                the format specified in its config.
            headers: additional HTTP headers to merge with provider-configured
                auth headers. Source-level headers are limited to
                Content-Type and idempotency markers; auth headers always
                come from config.

        Returns: ChannelSendResult with outcome "delivered" on 2xx,
            "failed" on non-2xx or network error (after retries),
            "queued" if the provider implements durable queuing.
        """
        ...


# ── Protocol: email ──


@runtime_checkable
class EmailChannelProvider(Protocol):
    """The email contract surface (BRD §6.4.2).

    Outbound email. SMTP/API credentials, default-from address, and
    reply-to live in provider config. Recipients are resolved by the
    runtime from principal claims before calling the provider — literal
    recipient lists never appear in source (leak-free principle).

    Action vocabulary in source:
        Subject is "<text>"
        Body is "<text>"
        HTML body is "<text>"       (optional)
        Attachments are <field>     (optional)
        Recipients are <role>       (resolved to email addresses by runtime)

    Apps using email implicitly require their identity provider to surface
    email claims. Phase 4 does not enforce this at compile time — flagged
    as a future lint.

    Provider config shape (bindings.channels.<name>.config):
        {
            "from": "noreply@example.com",
            "reply_to": "support@example.com",
            "api_key": "${SES_API_KEY}"
        }
    """

    async def send(
        self,
        recipients: Sequence[str],
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        attachments: Optional[Sequence[Any]] = None,
    ) -> ChannelSendResult:
        """Send an email.

        Args:
            recipients: list of resolved email addresses. The runtime
                resolves role → principal claims → email before calling;
                the provider receives concrete addresses only.
            subject: email subject line.
            body: plain-text body.
            html_body: optional HTML body. If supplied, providers should
                send a multipart/alternative message.
            attachments: optional list of attachment objects. Shape is
                provider-specific; callers should check provider docs.

        Returns: ChannelSendResult. "delivered" = accepted by SMTP/API;
            "queued" = accepted for async delivery; "failed" = rejected.
        """
        ...


# ── Protocol: messaging ──


@runtime_checkable
class MessagingChannelProvider(Protocol):
    """The messaging contract surface (BRD §6.4.3).

    Chat platform integration: Slack, Teams, Discord, Mattermost, etc.
    Supports outbound message send, message update, reaction, and inbound
    subscription. Provider declares which actions it implements via
    ProviderRecord.features; the runtime validates IR action vocab against
    the declared subset at startup.

    Action vocabulary in source:
        Send a message: `<CEL text expr>`
        Reply in thread to <message-ref> <text>
        Update message <message-ref> <text>
        React with <emoji> to <message-ref>
        When a message is received        (inbound trigger)
        When a reaction is added          (inbound trigger)
        When a thread reply is received   (inbound trigger)

    Deploy config shape (bindings.channels.<name>.config):
        {
            "workspace_token_ref": "${SLACK_BOT_TOKEN}",
            "target": "supplier-team-prod",
            "subscription": "supplier-team-prod"
        }
    The 'target' field is the physical channel identifier (Slack channel
    name, Discord channel ID, etc.). Source uses the logical channel name;
    deploy config resolves the physical target.
    """

    async def send(
        self,
        target: str,
        message_text: str,
        thread_ref: Optional[str] = None,
    ) -> MessageRef:
        """Send a message to the configured target.

        Args:
            target: physical platform target (resolved from deploy config
                by the ChannelDispatcher — not the source logical name).
            message_text: the message text. May include platform-native
                formatting markup (provider-specific).
            thread_ref: optional thread identifier for threaded replies.
                If None, sends as a top-level message.

        Returns: MessageRef with the platform-assigned message id, so
            callers can subsequently update or react to the message.
        """
        ...

    async def update(
        self,
        message_ref: str,
        new_text: str,
    ) -> None:
        """Update an existing message by its platform id.

        Args:
            message_ref: the id field from a previously-returned MessageRef.
            new_text: replacement text.
        """
        ...

    async def react(
        self,
        message_ref: str,
        emoji: str,
    ) -> None:
        """Add a reaction emoji to a message.

        Args:
            message_ref: the id field from a previously-returned MessageRef.
            emoji: emoji name or unicode character (provider-specific format).
        """
        ...

    async def subscribe(
        self,
        target: str,
        message_handler: Callable,
        reaction_handler: Optional[Callable] = None,
    ) -> Any:
        """Subscribe to inbound messages from the configured target.

        Args:
            target: physical platform target to subscribe to.
            message_handler: async callable invoked for each inbound
                message. Receives a dict with at minimum:
                {"text": str, "sender_id": str, "channel": str}.
            reaction_handler: optional async callable for reaction events.
                Receives {"emoji": str, "message_ref": str, "sender_id": str}.

        Returns: a subscription object with a cancel() method. Caller
            must call cancel() to stop receiving messages and release
            the platform subscription.
        """
        ...


# ── Protocol: event-stream ──


@runtime_checkable
class EventStreamChannelProvider(Protocol):
    """The event-stream contract surface (BRD §6.4.4).

    Server-sent events / WebSocket for external consumers that are NOT
    another Termin boundary. Internal Termin-to-Termin event propagation
    uses the distributed runtime layer (channel_ws.py), not this contract.

    Stub implementation deferred — no fixture requires it in Phase 4.
    Protocol defined here for completeness and to allow the contract
    registry to advertise the contract surface.

    Deploy config shape (bindings.channels.<name>.config):
        {
            "transport": "sse",
            "endpoint_path": "/streams/events",
            "auth": {"type": "bearer"}
        }
    """

    async def register_stream(
        self,
        name: str,
        content_types: Sequence[str],
        filter_predicate: Optional[Any] = None,
    ) -> str:
        """Register a named event stream and return its endpoint path.

        Args:
            name: logical stream name.
            content_types: content type names whose events are included.
            filter_predicate: optional Predicate AST to filter events.

        Returns: the endpoint path string (e.g., "/streams/supplier-events").
        """
        ...

    async def publish(
        self,
        stream_endpoint: str,
        event: Any,
    ) -> None:
        """Publish an event to a registered stream endpoint.

        Args:
            stream_endpoint: the path returned by register_stream().
            event: the event payload (provider serializes to JSON).
        """
        ...


# ── Action vocabulary — static contract tables ──


# Per design doc §7.4 (D): static action vocab table for compiler-side
# validation. The compiler checks that source action verbs on a Channel
# are drawn from this table when provider_contract is set.
#
# Each entry is a frozenset of display-string prefixes. The analyzer
# uses "action body starts with one of these" matching (display-string
# prefix match, per design decision D: "sufficient for Phase 4").
CHANNEL_CONTRACT_ACTION_VOCAB: dict[str, frozenset[str]] = {
    "webhook": frozenset({
        "Post",
    }),
    "email": frozenset({
        "Subject is",
        "Body is",
        "HTML body is",
        "Attachments are",
        "Recipients are",
    }),
    "messaging": frozenset({
        "Send a message",
        "Reply in thread to",
        "Update message",
        "React with",
        "When a message is received",
        "When a reaction is added",
        "When a thread reply is received",
    }),
    "event-stream": frozenset({
        "register_stream",
        "publish",
    }),
}

# Features list per contract — used to populate ProviderRecord.features
# for providers that implement the full vocabulary.
CHANNEL_CONTRACT_FULL_FEATURES: dict[str, tuple[str, ...]] = {
    "webhook": ("send",),
    "email": ("send",),
    "messaging": ("send", "update", "react", "subscribe"),
    "event-stream": ("register_stream", "publish"),
}


# ── Utility ──


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
