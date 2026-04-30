# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Framework-agnostic routing dispatch surface.

This package carries the value types adapters bind onto:

* :class:`TerminRequest` / :class:`TerminResponse` — what
  HTTP handlers consume / produce.
* :class:`TerminWebSocket` — the Protocol WebSocket handlers operate
  on; adapters wrap their framework's WS type.
* :class:`RouteSpec` / :class:`WebSocketRouteSpec` — declarative
  route descriptions; adapters loop and bind.

The actual handler implementations (CRUD list/get/create/update/
delete, channel dispatch, WebSocket subscription registry) live in
sibling modules added in slice 7.2.e and 7.2.f. This module
defines the substrate; the substrate is independent of any
framework, on top of ASGI semantics but without an ASGI library
dependency.
"""

from .request import TerminRequest, TerminResponse  # noqa: F401
from .websocket import TerminWebSocket  # noqa: F401
from .route_specs import (  # noqa: F401
    HttpHandler,
    WebSocketHandler,
    RouteSpec,
    WebSocketRouteSpec,
)

__all__ = [
    "TerminRequest",
    "TerminResponse",
    "TerminWebSocket",
    "HttpHandler",
    "WebSocketHandler",
    "RouteSpec",
    "WebSocketRouteSpec",
]
