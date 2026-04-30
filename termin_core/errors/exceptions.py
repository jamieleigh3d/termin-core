# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Framework-agnostic exception hierarchy for Termin runtimes.

These exceptions carry the information adapters need to translate a
runtime failure into their framework's error envelope. Each subclass
declares the HTTP-status-equivalent that an HTTP adapter should
emit; non-HTTP adapters (e.g., a CLI runtime, a JSON-RPC server)
can use the same hierarchy and translate however they like.

Distinguished from :class:`termin_core.errors.router.TerminError`,
which is a dataclass modeling the structured error *envelope* sent
back to the client. The exceptions here are how callers raise the
condition; the envelope is how it's serialized for transport.

Slice 7.2 of Phase 7 introduced this module so the previously
deferred validation / state / transitions modules could move out of
``termin_runtime/`` (which raises ``fastapi.HTTPException`` directly)
and into ``termin-core`` without taking on a FastAPI dependency.
"""

from typing import Any, Optional


class TerminRuntimeError(Exception):
    """Base class for framework-agnostic runtime failures.

    Carries an HTTP-status-equivalent class attribute (``status_code``)
    that HTTP adapters use to translate to their framework's error
    response. Non-HTTP adapters can ignore the status_code or map
    differently.

    The :attr:`detail` field carries the human-readable failure
    message; the :attr:`extra` dict carries any structured metadata
    the adapter wants to surface to the client (field name, allowed
    values, etc.).
    """

    status_code: int = 500

    def __init__(
        self,
        detail: str,
        *,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = dict(extra or {})

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status_code={self.status_code}, "
            f"detail={self.detail!r}, extra={self.extra!r})"
        )


class TerminBadRequestError(TerminRuntimeError):
    """400 Bad Request. The request is malformed in a way that isn't
    a validation or scope failure — bad query parameter, unknown
    state machine name, content type that doesn't exist, etc."""

    status_code = 400


class TerminScopeError(TerminRuntimeError):
    """403 Forbidden. The caller's scope set doesn't satisfy the
    scope required by the operation. Distinct from authentication
    failures (which are 401) — the caller IS authenticated, just
    not scoped for this operation."""

    status_code = 403


class TerminNotFoundError(TerminRuntimeError):
    """404 Not Found. A specific resource (record, content type,
    state-machine target, transition) doesn't exist."""

    status_code = 404


class TerminConflictError(TerminRuntimeError):
    """409 Conflict. The request conflicts with the resource's
    current state — an undeclared state transition, a foreign-key
    violation that prevents delete, an idempotency-key collision."""

    status_code = 409


class TerminValidationError(TerminRuntimeError):
    """422 Unprocessable Entity. Input data fails schema validation:
    a D-19 ``dependent_values`` rule, a ``one_of_values`` enum
    constraint, a min/max bound, a missing required field, a type
    mismatch."""

    status_code = 422


__all__ = [
    "TerminRuntimeError",
    "TerminBadRequestError",
    "TerminScopeError",
    "TerminNotFoundError",
    "TerminConflictError",
    "TerminValidationError",
]
