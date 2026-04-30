# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""v0.9 Phase 5a.1: Presentation contract surface.

Per BRD #2 (the presentation provider system) and the Phase 5 design
doc at docs/presentation-provider-design.md.

This module defines:
  - The PresentationProvider Protocol — single Protocol with
    discriminator on `contract` (design decision §3.1).
  - The closed presentation-base contract list — ten contracts per
    BRD §5.1 (page, text, markdown, data-table, form, chat, metric,
    nav-bar, toast, banner).
  - The Redacted sentinel for field-level redaction (§3.5 / BRD §7.6).
  - The PrincipalContext shape passed to every render (§3.11).
  - JSON encoder hook for Redacted so wire shapes stay consistent
    across SSR (HTML strings) and CSR (JSON-over-WebSocket) modes.

No behavior change. The existing termin_runtime/presentation.py
continues to drive rendering; Phase 5a.2 cuts over to provider-
keyed dispatch. This slice lands the contract layer so 5a.2 has
somewhere to plug into.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import (
    Any, Literal, Mapping, Optional, Protocol, runtime_checkable,
)


# ── Closed contract list (BRD #2 §5.1) ──

PRESENTATION_BASE_CONTRACTS: tuple[str, ...] = (
    "page",
    "text",
    "markdown",
    "data-table",
    "form",
    "chat",
    "metric",
    "nav-bar",
    "toast",
    "banner",
)
"""The ten contracts in the `presentation-base` namespace, fully
qualified as `presentation-base.<name>` when used in source. Phase 5b's
`Using` grammar references these by full name; 5c's contract packages
ship additional namespaces alongside this one."""


# ── Render modes (BRD §7.4) ──

RenderMode = Literal["ssr", "csr"]


# ── Field-level redaction sentinel (BRD §7.6 / design §3.5) ──

@dataclass(frozen=True)
class Redacted:
    """Type-safe sentinel for redacted fields (BRD #2 §7.6).

    Confidentiality filtering at the field level replaces values with
    a Redacted instance before the record reaches a presentation
    provider. The provider sees the marker, decides visual treatment
    (blank, ●●●, lock icon, etc.), and never sees the underlying
    value.

    Distinct from None / empty string / 0 / False so providers can
    discriminate redaction from natural absence. Custom JSON encoding
    via `redacted_json_default` produces a wire shape recognizable to
    CSR providers loading data over WebSocket.

    Fields:
      field_name: the snake-case field that was redacted.
      expected_type: the field's declared business type (text, number,
        currency, etc.) so providers can render an appropriate
        placeholder shape.
      reason: optional human-readable cause; provider may display.
    """
    field_name: str
    expected_type: str
    reason: Optional[str] = None


def redacted_json_default(obj: Any) -> Any:
    """JSON encoder hook for Redacted instances. Pass to
    json.dumps(default=...). Wire shape:

        {"__redacted": true, "field": <name>, "expected_type": <type>, "reason": <reason|null>}

    The leading `__redacted: true` lets clients (termin.js, future
    CSR providers) detect the marker without inspecting type tags.
    """
    if isinstance(obj, Redacted):
        return {
            "__redacted": True,
            "field": obj.field_name,
            "expected_type": obj.expected_type,
            "reason": obj.reason,
        }
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


def is_redacted(value: Any) -> bool:
    """Convenience: True iff value is a Redacted sentinel."""
    return isinstance(value, Redacted)


# ── PrincipalContext (design §3.11) ──

@dataclass(frozen=True)
class PrincipalContext:
    """The per-principal context passed to every render call.

    Mirrors the BRD #3 §4.2 `the user` Principal shape that source
    can reference via CEL expressions. The runtime constructs this
    from the request's resolved principal (delegating to the
    Identity provider) before invoking the bound presentation
    provider for a render site.

    Fields:
      principal_id: stable opaque id (BRD #3 §3.2 principal-typed).
      principal_type: "human" | "agent" | "service".
      role_set: roles the principal holds in this request.
      scope_set: scopes derived from roles (delegate-mode default).
      theme_preference: resolved theme value after `theme_locked`
        application — one of light | dark | auto | high-contrast.
      preferences: full preferences map; theme_preference is also
        present here as preferences["theme"] for ergonomic lookup.
      claims: opaque identity claims (provider-specific shape).
    """
    principal_id: str
    principal_type: str
    role_set: frozenset[str]
    scope_set: frozenset[str]
    theme_preference: str
    preferences: Mapping[str, Any]
    claims: Mapping[str, Any] = field(default_factory=dict)


# ── PresentationData (the bound data passed to render) ──

@dataclass(frozen=True)
class PresentationData:
    """The bound data passed to a presentation provider at render time.

    Construction is a runtime concern — the runtime fetches data
    referenced by the IR fragment, applies confidentiality filtering
    (replacing redacted fields with Redacted sentinels), and assembles
    the shape per the contract being rendered.

    Fields:
      records: rows fetched for tabular contracts (data-table, chat,
        metric field-grouped form). Empty for contracts that don't
        consume row data (text, page, nav-bar, toast, banner).
      props: contract-specific scalar/computed values (e.g., the
        `primary` number for a metric, the `severity` for a toast).
      meta: per-row metadata for data-table contracts — highlighted,
        available_actions, visible_actions, etc.
    """
    records: tuple[Mapping[str, Any], ...] = ()
    props: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)


# ── PresentationProvider Protocol (design §3.1) ──

@runtime_checkable
class PresentationProvider(Protocol):
    """The presentation provider contract.

    A single Protocol with a `contract` discriminator (rather than ten
    Protocols) — every contract in `presentation-base` shares the same
    conceptual shape: given a component-IR fragment + bound data +
    principal context, produce rendered output. See design doc §3.1.

    Provider attributes:
      declared_contracts: which fully-qualified contract names this
        provider implements. e.g., ("presentation-base.page",
        "presentation-base.text", ...). The runtime's deploy-time
        validation (BRD §8.3) checks every IR-required contract
        against this advertisement.
      render_modes: which of "ssr" or "csr" (or both) the provider
        supports. Carbon ships CSR-only; GOV.UK ships SSR-only;
        Tailwind-default ships both.

    Methods:
      render_ssr — server-side rendering. Called per component
        instance. Returns an HTML string the runtime composites into
        the page response.
      csr_bundle_url — for CSR-mode providers, return the URL of the
        JS bundle termin.js loads at page boot. None for SSR-only
        providers.
    """
    declared_contracts: tuple[str, ...]
    render_modes: tuple[RenderMode, ...]

    def render_ssr(
        self,
        contract: str,
        ir_fragment: Any,            # ComponentNode dict (lowered IR)
        data: PresentationData,
        principal_context: PrincipalContext,
    ) -> str:
        """Render one component-IR fragment to HTML.

        Called only when "ssr" in self.render_modes. Returns the
        rendered HTML string (already escape-safe per the contract's
        sanitization envelope — markdown sanitization, in particular,
        is applied by the runtime before the data reaches this
        method, per BRD §7.3).
        """
        ...

    def csr_bundle_url(self) -> Optional[str]:
        """Return the JS bundle URL for CSR-mode rendering.

        Called only when "csr" in self.render_modes. The runtime
        injects a <script> tag referencing this URL into the page
        boot HTML; termin.js loads the bundle, calls into its
        registered renderer functions for each component-IR
        fragment, and wires emitted actions back through the runtime.
        """
        ...


# ── Contract registry helper (design §3.6 / §4.1) ──

def register_presentation_base_contracts(contract_registry) -> None:
    """Register all ten presentation-base contracts in the given
    ContractRegistry. Called from register_builtins at app startup.

    The contract names are stored in the `presentation-base` namespace
    per BRD §10.4 ("Mandatory Using for non-default namespaces"). The
    base namespace is implicit in source — these names appear in the
    IR's required_contracts field via verb-to-contract mapping during
    lowering, not via explicit `Using` clauses.
    """
    from .contracts import (
        Category, ContractDefinition, Tier,
    )
    for name in PRESENTATION_BASE_CONTRACTS:
        contract_registry.register_contract(ContractDefinition(
            name=f"presentation-base.{name}",
            category=Category.PRESENTATION,
            tier=Tier.TIER_2,
            naming="named",
            description=(
                f"presentation-base contract `{name}` — see BRD #2 §5.1 "
                f"and docs/presentation-provider-design.md."
            ),
        ))


__all__ = [
    "PRESENTATION_BASE_CONTRACTS",
    "RenderMode",
    "Redacted",
    "redacted_json_default",
    "is_redacted",
    "PrincipalContext",
    "PresentationData",
    "PresentationProvider",
    "register_presentation_base_contracts",
]
