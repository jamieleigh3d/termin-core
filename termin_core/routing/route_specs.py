# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""RouteSpec — declarative description of a runtime route.

Per Q2 of the Phase 7 routing briefing: termin-core describes
routes as a list of :class:`RouteSpec` rather than as decorator
side-effects. The runtime walks the IR and produces a list; the
adapter consumes the list and binds each spec to its framework's
router. The list is inspectable by the conformance pack and works
the same way for decorator-shaped and non-decorator-shaped hosts.

This module only defines the value types. The functions that
produce a list of RouteSpecs by walking an IR (e.g.,
``build_crud_routes(ctx)``) are added in slice 7.2.e alongside
each handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .request import TerminRequest, TerminResponse
from .websocket import TerminWebSocket


# Type aliases for the two handler shapes adapters bind:
HttpHandler = Callable[[TerminRequest, Any], Awaitable[TerminResponse]]
WebSocketHandler = Callable[[TerminWebSocket, Any], Awaitable[None]]


@dataclass
class RouteSpec:
    """One HTTP route the runtime exposes.

    Adapters consume a ``list[RouteSpec]`` and bind each spec to
    their framework's router. The standard binding loop is::

        for spec in routes:
            adapter.add_route(
                method=spec.method,
                path=spec.path,
                handler=_wrap(spec, ctx),
                description=spec.description,
            )

    Attributes:
        method: HTTP method (``GET``, ``POST``, ``PUT``, ``DELETE``,
            ``PATCH``). Uppercase.
        path: URL pattern. Path parameters use ``{name}`` syntax —
            adapters translate to their framework's pattern
            language (Starlette and FastAPI accept ``{name}``
            verbatim; other frameworks may need translation).
        handler: Async coroutine taking a :class:`TerminRequest`
            and the runtime ``ctx``, returning a
            :class:`TerminResponse`.
        required_scope: When set, the adapter's scope-enforcement
            middleware checks the principal carries this scope
            before invoking the handler. None means publicly
            accessible (still subject to identity / boundary checks
            elsewhere).
        description: Human-readable description for OpenAPI / docs.
    """

    method: str
    path: str
    handler: HttpHandler
    required_scope: Optional[str] = None
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.method = self.method.upper()


@dataclass
class WebSocketRouteSpec:
    """One WebSocket route the runtime exposes.

    Same shape as :class:`RouteSpec` but for WS endpoints. Adapters
    bind each spec to their framework's WebSocket route registration.

    Attributes:
        path: URL pattern, ``{name}`` placeholders supported.
        handler: Async coroutine taking a :class:`TerminWebSocket`
            and the runtime ``ctx``. Returns when the connection
            closes; the adapter ensures the underlying socket is
            torn down whether the handler returns normally or
            raises.
        required_scope: Optional scope check on connect. The
            adapter's middleware closes the connection with code
            4403 (custom-app forbidden) before the handler runs if
            the principal lacks the scope.
        description: Human-readable description.
    """

    path: str
    handler: WebSocketHandler
    required_scope: Optional[str] = None
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


__all__ = [
    "HttpHandler",
    "WebSocketHandler",
    "RouteSpec",
    "WebSocketRouteSpec",
]
