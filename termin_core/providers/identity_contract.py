# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Identity contract surface — v0.9 Phase 1.

Per BRD §6.1, the Identity contract has a single contract surface
(category=identity, name="default") with two operations:

    authenticate(credentials) -> Principal
    roles_for(principal, app_id) -> Set<RoleName>

This module declares the typed shapes (Principal, RoleName) and the
Protocol interface that any identity provider implementation must
satisfy. Concrete providers (stub, future SSO/OIDC, etc.) live in
termin_runtime/providers/builtins/ or in third-party packages.

Behavioral requirements (BRD §6.1) the runtime enforces around
provider calls — provider implementations may rely on these:

  - Anonymous bypasses the provider entirely. No-credentials requests
    never call authenticate; the runtime constructs the Anonymous
    Principal directly.
  - Fail-closed default: if the provider raises or times out, no
    roles can be resolved and only Anonymous-permitted operations
    succeed.
  - Mid-session role changes are enforced — the runtime queries
    roles_for on the principal as needed; providers may not return
    stale role lists.
  - Multi-role principals are first-class. Effective scopes are the
    union of source-declared scopes for each role the provider
    returns.
  - Service principals (type="service") have their own roles via
    role_mappings (deploy config). Agent principals in delegate mode
    have no roles of their own — authorization derives from
    on_behalf_of. In service mode, agents have their own roles, no
    on_behalf_of.

The contract itself is symbol-environment-agnostic: it deals with
authentication and role resolution, not scope computation. The
runtime translates roles → scopes using the source's
Identity-block role-to-scope mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


# Role names are strings — the contract doesn't constrain their shape
# beyond "something a source can declare in an Identity: block."
# Type aliased here for documentation and forward-compatibility (e.g.,
# if we ever add hierarchical role names like "org.team.role").
RoleName = str


@dataclass(frozen=True)
class Principal:
    """The identity contract's representation of who is acting.

    Stable across the principal's lifetime — id never changes once a
    principal is established. Subsequent authentications for the same
    underlying entity (e.g., the same human re-logging-in) resolve to
    the same id.

    Fields:
      id: stable identifier opaque to the runtime. Provider chooses
          the encoding (cookie hash, OIDC sub, AWS IAM ARN, etc.).
      type: 'human' | 'agent' | 'service'.
      display_name: human-readable name for audit logs and UI. May be
          empty for service principals where no name is meaningful.
      claims: open map of provider-supplied facts. No required keys
          per the BRD — `email`, `oidc_sub`, `groups`, etc. are all
          optional. Apps that depend on a claim (e.g., email channel
          requires `email`) lint the dependency at compile time.
      on_behalf_of: for delegate-mode agents, the human Principal the
          agent acts on behalf of. None for human, service, or
          service-mode agent principals.
      preferences: per-principal extensible key-value store (BRD #3
          §4.2). Theme preference lives here as `preferences["theme"]`
          (supersedes BRD #2 §6.2's top-level theme_preference field).
          Identity providers populate when known; runtime read/writes
          in Phase 5a's theme-preference plumbing.
      is_system: True for synthetic system principals (scheduled jobs,
          etc.). v0.9 has no source path that creates a system
          principal — the field exists for forward compatibility with
          v0.10+'s Trigger on schedule (Phase 6a). Per BRD #3 §4.4.
    """
    id: str
    type: str  # "human" | "agent" | "service"
    display_name: str = ""
    claims: Mapping[str, Any] = field(default_factory=dict)
    on_behalf_of: Optional["Principal"] = None
    # v0.9 Phase 6a.4 (BRD #3 §4.2):
    preferences: Mapping[str, Any] = field(default_factory=dict)
    is_system: bool = False

    def __post_init__(self) -> None:
        if self.type not in ("human", "agent", "service"):
            raise ValueError(
                f"Principal.type must be 'human' | 'agent' | 'service', "
                f"got {self.type!r}"
            )
        if self.type == "agent" and self.on_behalf_of is not None:
            # Delegate mode — on_behalf_of must be a human or service.
            if self.on_behalf_of.type == "agent":
                raise ValueError(
                    "Agent.on_behalf_of must be a human or service Principal, "
                    "not another agent (no agent-of-agent delegation chains "
                    "in v0.9)."
                )

    @property
    def is_anonymous(self) -> bool:
        """True iff this is the canonical Anonymous principal."""
        return self.id == "anonymous"


# Sentinel used by the runtime to short-circuit provider calls when
# no credentials are supplied. Per BRD §6.1, Anonymous bypasses the
# provider entirely.
ANONYMOUS_PRINCIPAL = Principal(
    id="anonymous",
    type="human",
    display_name="Anonymous",
    claims={},
    on_behalf_of=None,
)


@runtime_checkable
class IdentityProvider(Protocol):
    """The Identity contract surface.

    Providers implement this Protocol against the BRD §6.1 contract.
    The runtime never instantiates Principals directly except for the
    Anonymous sentinel — all other identities flow through
    authenticate.

    Provider config (deploy_config["bindings"]["identity"]["config"])
    is supplied to the provider's factory function at construction
    time, not on every call. role_mappings
    (deploy_config["bindings"]["identity"]["role_mappings"]) is owned
    by the runtime — providers see the resolved roles list, not the
    mapping that produced it.
    """

    def authenticate(self, credentials: Mapping[str, Any]) -> Principal:
        """Resolve credentials to a Principal.

        Called only when the runtime has detected credentials in the
        request — never with empty/null credentials. Per BRD §6.1,
        Anonymous requests bypass this method entirely.

        credentials: provider-specific shape. The stub provider
            expects a dict with 'role' (cookie-style) and optional
            'user_name'. SSO providers might expect an OIDC token,
            etc.

        Raises:
            an exception if credentials are malformed or the provider
            cannot reach its backend. The runtime treats failures as
            fail-closed: the request gets the Anonymous Principal and
            only Anonymous-permitted operations succeed.
        """
        ...

    def roles_for(self, principal: Principal, app_id: str) -> set:
        """Return the set of role names this principal holds for the
        given app.

        For Anonymous, the runtime does NOT call this method —
        Anonymous's roles come from source (the `Anonymous has "..."`
        line). Providers may safely raise if asked to resolve roles
        for the Anonymous principal.

        Per BRD §6.1, providers must return the freshest role list
        they can supply. TTL caching is allowed but stale roles are a
        contract violation; the runtime expects mid-session role
        changes to take effect by the next call.
        """
        ...
