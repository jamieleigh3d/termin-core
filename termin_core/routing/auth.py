# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""AuthContext — the routing-layer view of "who's making this request
and what can they do here."

Splits two concepts the legacy ``ctx.get_current_user(request)`` dict
conflated:

* :class:`termin_core.providers.identity_contract.Principal` — *who*
  the caller is (stable identity, opaque id, type, claims). Lives
  next to the IdentityProvider Protocol because providers produce it.
* :class:`AuthContext` — Principal *plus* the scopes that role-mapping
  has resolved for this request, plus the role name for back-compat.
  Lives in the routing layer because it's request-scoped data the
  adapter assembles before each handler runs.

Per Q3=a of the routing briefing: adapter middleware computes the
AuthContext once at the boundary and sets ``request.auth`` before
the handler runs. Handlers consume ``request.auth.principal`` for
identity questions and ``request.auth.has_scope(...)`` for
authorization questions.

Slice 7.2.e of Phase 7 (2026-04-30) introduced this type to replace
the loose ``user: dict`` shape that previously flowed through
``ctx.get_current_user(request)``. The legacy dict drops in slice
7.5 once every handler has migrated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..providers.identity_contract import Principal


@dataclass(frozen=True)
class AuthContext:
    """Resolved authentication context for one request.

    Adapters construct an AuthContext once per request via:

    * Reading the framework's auth state (cookies, OIDC claims, JWT,
      etc.) and translating to a :class:`Principal` via the bound
      identity provider.
    * Mapping the Principal's role assignment to the scope set the
      app's IR declares for that role.
    * Snapshotting the role name for legacy back-compat (audit log
      lines, error messages, etc. that name the role).

    Attributes:
        principal: Stable identity. Must always be set; for
            unauthenticated requests use the anonymous principal
            from ``termin_core.providers.identity_contract``
            (``ANONYMOUS_PRINCIPAL``).
        scopes: Tuple of scope names this principal has *for this
            request* — boundary-scoped, role-mapped, app-declared.
            Tuple (not set) so the value is hashable.
        roles: Tuple of role names assigned to this principal in this
            app's identity block. v0.9 cookie-based runtime resolves a
            single role and produces a 1-tuple; future identity
            providers (Okta groups, OIDC claims, etc.) populate the
            full assigned set. Source CEL reads via ``the user.roles``.
        role_name: The legacy single-role label. Always equal to
            ``roles[0] if roles else ""``. Kept for the audit-log
            and error-message call sites that name a single role;
            consider dropping in v1.0 once those call sites move to
            ``roles``.
    """

    principal: "Principal"
    scopes: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    role_name: str = ""

    def __post_init__(self) -> None:
        # Keep ``role_name`` in sync with ``roles[0]`` for the legacy
        # call sites. Adapters that only know the legacy single-role
        # spelling can keep passing ``role_name=...`` and roles
        # auto-populates; conversely, callers that pass ``roles=(...)``
        # get role_name backfilled. ``object.__setattr__`` is the
        # frozen-dataclass-safe way to do this.
        if self.roles and not self.role_name:
            object.__setattr__(self, "role_name", self.roles[0])
        elif self.role_name and not self.roles:
            object.__setattr__(self, "roles", (self.role_name,))

    def has_scope(self, name: str) -> bool:
        """True if the named scope is in :attr:`scopes`. The
        canonical authorization check; equivalent to
        ``name in self.scopes`` but the named method documents intent
        at every call site."""
        return name in self.scopes

    def has_any(self, names: "tuple[str, ...] | list[str] | set[str]") -> bool:
        """True if any of the given scopes is in :attr:`scopes`."""
        return any(n in self.scopes for n in names)

    def has_all(self, names: "tuple[str, ...] | list[str] | set[str]") -> bool:
        """True if all of the given scopes are in :attr:`scopes`."""
        return all(n in self.scopes for n in names)

    @property
    def is_anonymous(self) -> bool:
        """True if the principal is the anonymous principal. The
        adapter constructs an AuthContext with the anonymous
        principal when no auth context can be determined.

        The anonymous principal's id is the literal string
        ``"anonymous"`` per
        :data:`termin_core.providers.identity_contract.ANONYMOUS_PRINCIPAL`.
        Empty-id principals would also be treated as anonymous (no
        identity provider should produce them, but the check is
        defensive).
        """
        return self.principal.id in ("", "anonymous")

    @property
    def is_system(self) -> bool:
        """True if the principal is a synthetic system principal
        (scheduled jobs, etc.) — propagated from
        :attr:`Principal.is_system`."""
        return getattr(self.principal, "is_system", False)


def build_the_user_for_cel(auth: "AuthContext | None") -> dict:
    """Build the BRD #3 §4.2-shaped ``the user`` binding for CEL.

    Slice 7.5b (2026-04-30): every CEL evaluator site that historically
    bound a ``User`` PascalCase dict now binds a single ``the_user``
    key whose value is this dict. Source CEL spells it as
    ``the user.X`` or, after the optional-``the`` rewrite, plain
    ``user.X`` — both resolve to ``the_user`` in the eval context.

    Returns the same shape ``_build_the_user_object`` builds in
    termin-server/identity.py, but without depending on the runtime's
    Principal record (this lives in core; identity is a Protocol
    surface). The two builders agree by construction — any field
    added to one must be added to the other.

    Anonymous fallback: when ``auth`` is None, returns a dict whose
    ``is_anonymous`` is True so source CEL doesn't NPE when an
    unauthenticated request lands.
    """
    if auth is None:
        return {
            "id": "anonymous",
            "display_name": "",
            "is_anonymous": True,
            "is_system": False,
            "scopes": [],
            "roles": [],
            "preferences": {},
        }
    p = auth.principal
    return {
        "id": p.id,
        "display_name": p.display_name or "",
        "is_anonymous": auth.is_anonymous,
        "is_system": auth.is_system,
        "scopes": list(auth.scopes),
        "roles": list(auth.roles),
        "preferences": dict(getattr(p, "preferences", {}) or {}),
    }


__all__ = ["AuthContext", "build_the_user_for_cel"]
