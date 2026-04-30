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
        role_name: The role label the legacy runtime threaded through.
            Empty string for principals not mapped to a named role.
            Slice 7.5 considers whether this stays or drops.
    """

    principal: "Principal"
    scopes: tuple[str, ...] = ()
    role_name: str = ""

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


__all__ = ["AuthContext"]
