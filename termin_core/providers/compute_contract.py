# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Compute contract surfaces — v0.9 Phase 3.

Per BRD §6.3, the Compute category has three named contracts (the first
multi-contract category). Each contract has a different operation
signature, so each gets its own Protocol — `default-CEL` evaluates pure
expressions, `llm` runs single-shot prompt completion, `ai-agent`
runs autonomous tool-using behavior with streaming.

Contract → Protocol mapping:

    (compute, "default-CEL") → DefaultCelComputeProvider
    (compute, "llm")         → LlmComputeProvider
    (compute, "ai-agent")    → AiAgentComputeProvider

Concrete providers (default-cel, anthropic, stub) live in
`termin_runtime/providers/builtins/`. The compute_runner constructs a
provider per compute via the ProviderRegistry, looking up by the
source's `Provider is "<name>"` line (or `default-CEL` if absent) and
the deploy config's `bindings.compute["<compute-name>"].provider`.

Also declared here: the data shapes the contracts trade in —
`CompletionResult`, `AgentResult`, `AgentContext`, `ToolSurface`,
`AgentEvent` variants, `AuditRecord`. Per BRD §6.3.4, every llm/agent
invocation produces an AuditRecord; the runtime persists it through
the auto-generated audit Content.

Behavioral requirements (BRD §6.3) the runtime enforces around provider
calls — provider implementations may rely on these:

  - **Tool calls always go through the gate.** Providers receive a
    `ToolSurface` with the tool functions already gated. They cannot
    invoke a tool the source did not declare; the gate raises
    `ToolNotDeclared` (TERMIN-A001) before reaching the implementation.
  - **Refusal is a tool call.** The agent invokes `system.refuse(reason)`
    to refuse work. The provider's invocation result returns
    `outcome="refused"` with the reason populated. Runtime aborts the
    agent loop on refusal; staged writes roll back.
  - **Audit is mandatory for refused outcomes** regardless of the
    compute's `audit_level`. Auditors must always see refusals.
  - **Streaming is opt-in.** Providers MAY implement
    `invoke_streaming`; runtime uses it when available and falls back
    to `invoke` otherwise. AgentEvent variants are runtime-translated
    to the existing `compute.stream.<inv_id>.*` event-bus channels.
  - **Service-mode and delegate-mode authorization derive from
    `AgentContext.principal`.** Delegate mode: principal has
    `on_behalf_of` set; effective scopes come from the on-behalf-of
    principal. Service mode: principal is the agent's own service
    principal with role_mappings-derived scopes.

The contract itself is provider-agnostic. Each named contract knows its
own operation signature; providers translate to whatever LLM/SDK shape
they use under the hood.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any, AsyncIterator, Callable, Mapping, Optional, Protocol,
    Sequence, Union, runtime_checkable,
)

from .identity_contract import Principal


# ── Tool surface and gating ──


@dataclass(frozen=True)
class ToolSurface:
    """The closed set of tool calls available to an ai-agent invocation.

    Computed at lower-time from the ComputeSpec's source-level
    declarations (`Accesses`, `Reads`, `Sends to`, `Emits`, `Invokes`)
    plus the always-available tools (`identity.self`, `system.refuse`).
    Frozen — providers receive this and cannot extend it.

    Per BRD §6.3.3, the tool surface is closed: providers cannot reach
    around it to call tools the source did not declare. The gate
    function is the single authorization point; providers see only
    what passed both gates (declared in source, principal authorized).

    Field shape:
      content_rw: tuple of content type names with full CRUD + state
          (granted by `Accesses <T>`).
      content_ro: tuple of content type names with read-only access
          (granted by `Reads <T>`). State tools NOT granted.
      channels: tuple of channel names the agent can `channel.send`
          and `channel.invoke_action` against (granted by
          `Sends to "<C>" channel`).
      events: tuple of event names the agent can `event.emit`
          (granted by `Emits "<E>"`).
      computes: tuple of compute names the agent can `compute.invoke`
          (granted by `Invokes "<X>"`).
      always_available: tuple of always-available tool names —
          `identity.self`, `system.refuse`. Included here so audit /
          conformance tests can assert the full surface declaratively.

    The ai-agent provider reads these fields to construct the tool
    schemas it sends to the LLM. The runtime's gate function consults
    them on each tool call to decide whether to dispatch.
    """
    content_rw: tuple[str, ...] = ()
    content_ro: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    computes: tuple[str, ...] = ()
    always_available: tuple[str, ...] = ("identity.self", "system.refuse")

    def permits_content_read(self, content_type: str) -> bool:
        """Read-side check: type appears in either Accesses or Reads."""
        return content_type in self.content_rw or content_type in self.content_ro

    def permits_content_write(self, content_type: str) -> bool:
        """Write-side check: type appears in Accesses only."""
        return content_type in self.content_rw

    def permits_state_transition(self, content_type: str) -> bool:
        """State tools come from Accesses only — never from Reads.
        BRD §6.3.3 explicit."""
        return content_type in self.content_rw

    def permits_channel(self, channel_name: str) -> bool:
        return channel_name in self.channels

    def permits_event(self, event_name: str) -> bool:
        return event_name in self.events

    def permits_compute(self, compute_name: str) -> bool:
        return compute_name in self.computes


# ── Audit record (BRD §6.3.4) ──


@dataclass(frozen=True)
class ToolCall:
    """One tool call's audit detail. BRD §6.3.4."""
    tool: str                          # e.g., "content.query", "system.refuse"
    args: Mapping[str, Any]
    result: Any                        # may be a serialized error
    is_error: bool = False
    latency_ms: int = 0


@dataclass(frozen=True)
class Cost:
    """Provider-reported cost. Optional; not all providers report.
    BRD §6.3.4."""
    units: int                         # tokens, requests, etc.
    unit_type: str                     # "tokens" | "requests" | ...
    currency_amount: Optional[str] = None  # numeric string in USD or similar


@dataclass(frozen=True)
class AuditRecord:
    """The contract-level audit shape per BRD §6.3.4.

    Every llm and ai-agent invocation produces one of these. The
    runtime persists it through the auto-generated audit Content type;
    operators may access it according to the compute's declared audit
    scope.

    Note: the persistence layer (audit Content schema) flattens this
    into columns. This dataclass is the contract-level shape providers
    return; runtime translation is in compute_runner.write_audit_trace.

    `provider_config_hash` strategy (per design Q3): canonical-JSON hash
    of the config dict with secret values replaced by their key paths.
    Same operational config → same hash regardless of API key rotation.
    Different products with same secret name → different hash.
    """
    provider_product: str              # e.g., "anthropic", "stub"
    model_identifier: str              # e.g., "claude-haiku-4-5-20251001"
    provider_config_hash: str

    prompt_as_sent: str                # full assembled prompt
    sampling_params: Mapping[str, Any] = field(default_factory=dict)

    tool_calls: tuple[ToolCall, ...] = ()  # ai-agent only; () for llm
    outcome: str = "success"           # "success" | "refused" | "error"
    refusal_reason: Optional[str] = None
    error_detail: Optional[str] = None

    cost: Optional[Cost] = None
    latency_ms: int = 0

    def __post_init__(self) -> None:
        if self.outcome not in ("success", "refused", "error"):
            raise ValueError(
                f"AuditRecord.outcome must be 'success' | 'refused' | "
                f"'error', got {self.outcome!r}"
            )
        if self.outcome == "refused" and not self.refusal_reason:
            raise ValueError(
                "AuditRecord with outcome='refused' must have a "
                "non-empty refusal_reason."
            )
        if self.outcome == "error" and not self.error_detail:
            raise ValueError(
                "AuditRecord with outcome='error' must have a "
                "non-empty error_detail."
            )


# ── llm contract result ──


@dataclass(frozen=True)
class CompletionResult:
    """Result of an `llm.complete` call. BRD §6.3.2.

    Single-shot. No tool surface. Refusal supported via
    outcome="refused" + refusal_reason. Streaming is the same shape;
    runtime accumulates token deltas into output_value before returning.
    """
    outcome: str                       # "success" | "refused" | "error"
    output_value: Any = None
    refusal_reason: Optional[str] = None
    error_detail: Optional[str] = None
    audit_record: Optional[AuditRecord] = None

    def __post_init__(self) -> None:
        if self.outcome not in ("success", "refused", "error"):
            raise ValueError(
                f"CompletionResult.outcome must be one of 'success' | "
                f"'refused' | 'error', got {self.outcome!r}"
            )


# ── ai-agent contract result and context ──


@dataclass(frozen=True)
class AuditableAction:
    """One audited action the agent took during an invocation.
    Flat shape — the AuditRecord's tool_calls list carries the full
    detail; this is for the AgentResult's actions_taken summary."""
    tool: str
    target: Optional[str] = None       # e.g., the content type or channel name
    succeeded: bool = True


@dataclass(frozen=True)
class AgentResult:
    """Result of an `ai-agent.invoke` call. BRD §6.3.3."""
    outcome: str                       # "success" | "refused" | "error"
    actions_taken: tuple[AuditableAction, ...] = ()
    reasoning_summary: Optional[str] = None
    refusal_reason: Optional[str] = None
    error_detail: Optional[str] = None
    audit_record: Optional[AuditRecord] = None
    output_value: Any = None           # final tool-call output, if any

    def __post_init__(self) -> None:
        if self.outcome not in ("success", "refused", "error"):
            raise ValueError(
                f"AgentResult.outcome must be one of 'success' | "
                f"'refused' | 'error', got {self.outcome!r}"
            )


# A tool callable as the runtime presents it to the provider. The
# provider passes (name, args) and gets back a result dict. The runtime
# has already gated the call; the provider sees only declared+authorized
# tools.
ToolCallback = Callable[[str, Mapping[str, Any]], Any]


@dataclass(frozen=True)
class AgentContext:
    """Per-invocation context the ai-agent provider receives. BRD §6.3.3.

    principal: the effective principal for authorization. In delegate
        mode this is the agent's principal with on_behalf_of set; the
        runtime's gate function uses `on_behalf_of`'s scopes when
        delegate mode is in effect. In service mode this is the agent's
        own service principal.
    bound_symbols: source-level symbols available to the invocation
        (input field values, triggering record, etc.).
    tool_callback: the gated tool dispatcher. Provider invokes via
        `await tool_callback(tool_name, tool_args)`; each call passes
        through the gate. ToolNotDeclared / NotAuthorized are
        runtime-translated exceptions the provider can catch and
        propagate to the LLM.
    """
    principal: Principal
    bound_symbols: Mapping[str, Any] = field(default_factory=dict)
    tool_callback: Optional[ToolCallback] = None


# ── Streaming events (BRD §6.3.3) ──


@dataclass(frozen=True)
class TokenEmitted:
    """A token / partial-text fragment from the agent."""
    text: str


@dataclass(frozen=True)
class ToolCalled:
    """The agent invoked a tool. Args are pre-gate; ToolResult follows."""
    tool: str
    args: Mapping[str, Any]
    call_id: str


@dataclass(frozen=True)
class ToolResult:
    """The result of a previously-Called tool. Matched by call_id."""
    call_id: str
    result: Any
    is_error: bool = False


@dataclass(frozen=True)
class Completed:
    """The agent finished. Result carries outcome + actions + reasoning."""
    result: AgentResult


@dataclass(frozen=True)
class Failed:
    """The agent's invocation failed (provider/transport/SDK error).
    Distinct from outcome="refused" — refusal goes through Completed
    with result.outcome="refused"."""
    error: str


AgentEvent = Union[TokenEmitted, ToolCalled, ToolResult, Completed, Failed]


# ── Exceptions raised by the gate function ──


class ToolNotDeclared(Exception):
    """TERMIN-A001: the agent attempted a tool the source did not
    declare in `Accesses` / `Reads` / `Sends to` / `Emits` / `Invokes`.

    Runtime catches this, records a denied tool call in the audit, and
    surfaces a structured error to the agent (so the agent can adjust
    its plan rather than crashing the invocation)."""

    def __init__(self, tool_name: str, target: Optional[str] = None):
        self.tool_name = tool_name
        self.target = target
        suffix = f" (target={target!r})" if target else ""
        super().__init__(
            f"TERMIN-A001: tool {tool_name!r}{suffix} not declared "
            f"in source. Add the corresponding access grant to the "
            f"Compute block."
        )


class NotAuthorized(Exception):
    """TERMIN-A002: the principal lacks the required scope to perform
    a declared tool call. Distinct from ToolNotDeclared — the source
    permits the action; the running principal does not."""

    def __init__(self, tool_name: str, required_scope: str):
        self.tool_name = tool_name
        self.required_scope = required_scope
        super().__init__(
            f"TERMIN-A002: tool {tool_name!r} requires scope "
            f"{required_scope!r}; principal does not have it."
        )


# ── The three Protocols ──


@runtime_checkable
class DefaultCelComputeProvider(Protocol):
    """The default-CEL contract surface (BRD §6.3.1).

    Pure expression evaluation. Synchronous, deterministic. Implicit
    contract — applies when source has no `Provider is` line. The
    runtime ships exactly one product (`default-cel`) wrapping
    cel-python; third-party CEL evaluators can register against this
    contract too.

    The contract is symbol-environment-agnostic: callers supply the
    bound symbols. Runtime sites (compute body, trigger filter, event
    handler condition, pre/postcondition) supply different
    environments; the provider doesn't know which it's serving.
    """

    def evaluate(
        self,
        expression: str,
        bound_symbols: Mapping[str, Any],
    ) -> Any:
        """Evaluate the CEL expression against the bound symbols.

        Raises if the expression has a syntax error or references an
        unbound symbol; the runtime translates into a structured
        compile/runtime error appropriate to the call site.
        """
        ...


@runtime_checkable
class LlmComputeProvider(Protocol):
    """The llm contract surface (BRD §6.3.2).

    Single-shot prompt → completion. No tool surface. Refusal supported
    via CompletionResult.outcome="refused". Streaming is implementation
    detail of `complete` — runtime accumulates token deltas; callers
    that want partial-update events get them via the existing
    `compute.stream.<inv_id>.field.<name>` event bus.

    Provider config (deploy_config["bindings"]["compute"]["<name>"]
    ["config"]) is supplied to the factory at construction time; the
    runtime does not pass it on every call.
    """

    async def complete(
        self,
        directive: str,
        objective: str,
        input_value: Any,
        output_schema: Optional[Mapping[str, Any]] = None,
        sampling_params: Optional[Mapping[str, Any]] = None,
    ) -> CompletionResult:
        """Run a single-shot completion.

        Args:
            directive: system-prompt-shaped directive from source.
            objective: user-prompt-shaped objective from source.
            input_value: the input bound from `Input from field <X>`,
                already resolved against the triggering record.
            output_schema: optional JSON-schema-shaped dict describing
                the expected output structure. When supplied, the
                provider should constrain the model to produce output
                matching the schema (Anthropic uses tool_use forcing;
                OpenAI uses structured-output JSON schema). When None,
                the provider returns free-form text.
            sampling_params: temperature/top_p/seed if supplied;
                provider may pass through to its SDK or ignore.

        Returns: CompletionResult with outcome, output_value (on
        success — a dict matching output_schema if one was supplied,
        else free-form text) or refusal_reason (on refused) or
        error_detail (on error), and an AuditRecord stamped with the
        provider's reproducibility fields.
        """
        ...


@runtime_checkable
class AiAgentComputeProvider(Protocol):
    """The ai-agent contract surface (BRD §6.3.3).

    Multi-action autonomous behavior with closed tool surface. The
    provider receives the gated `tool_callback` in `AgentContext` —
    every tool call passes through the runtime's gate (BRD §6.3.3
    "double-gated"). Refusal is a first-class tool call
    (`system.refuse`); the runtime translates it into AgentResult with
    outcome="refused" before returning to the caller.

    Streaming is opt-in. Providers that implement `invoke_streaming`
    yield AgentEvent variants in real time; runtime translates each
    to an event-bus event. Providers that don't implement streaming
    only need `invoke`; the runtime falls back transparently.
    """

    async def invoke(
        self,
        directive: str,
        objective: str,
        context: AgentContext,
        tools: ToolSurface,
    ) -> AgentResult:
        """Run the agent to completion (non-streaming).

        Args:
            directive: system-prompt-shaped directive from source.
            objective: user-prompt-shaped objective from source.
            context: principal + bound symbols + gated tool_callback.
            tools: closed tool surface the provider may construct
                schemas for.

        Returns: AgentResult with outcome, actions_taken, reasoning,
        and an AuditRecord including the full tool_calls list.
        """
        ...

    async def invoke_streaming(
        self,
        directive: str,
        objective: str,
        context: AgentContext,
        tools: ToolSurface,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent and yield AgentEvent variants in real time.

        The final event MUST be either Completed (carrying the same
        AgentResult `invoke` would return) or Failed (carrying an
        error string distinct from outcome="refused"; refusal flows
        through Completed with result.outcome="refused").

        Implementations that don't natively stream MAY simulate by
        running `invoke` and yielding a single Completed event;
        runtime treats this as equivalent.
        """
        ...
