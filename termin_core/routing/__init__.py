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

from .auth import AuthContext, build_the_user_for_cel  # noqa: F401
from .request import TerminRequest, TerminResponse  # noqa: F401
from .websocket import TerminWebSocket  # noqa: F401
from .route_specs import (  # noqa: F401
    HttpHandler,
    WebSocketHandler,
    RouteSpec,
    WebSocketRouteSpec,
)
from .crud import (  # noqa: F401
    create_content_handler,
    delete_content_handler,
    get_content_handler,
    list_content_handler,
    transition_content_handler,
    update_content_handler,
)
from .connection_manager import (  # noqa: F401
    ConnectionManager,
    filter_owned_rows,
)
from .channel_dispatch import dispatch_websocket_session  # noqa: F401
from .compute import trigger_compute_handler  # noqa: F401
from .channels import (  # noqa: F401
    channel_send_handler,
    invoke_channel_action_handler,
    webhook_receive_handler,
)
from .append import (  # noqa: F401
    CANONICAL_KINDS,
    AppendValidationError,
    AppendNotFoundError,
    append_to_field,
)
from .dispatch import (  # noqa: F401
    build_route_specs,
    dispatch_http_request,
)

__all__ = [
    "AuthContext",
    "build_the_user_for_cel",
    "TerminRequest",
    "TerminResponse",
    "TerminWebSocket",
    "HttpHandler",
    "WebSocketHandler",
    "RouteSpec",
    "WebSocketRouteSpec",
    "create_content_handler",
    "delete_content_handler",
    "get_content_handler",
    "list_content_handler",
    "transition_content_handler",
    "update_content_handler",
    "ConnectionManager",
    "filter_owned_rows",
    "dispatch_websocket_session",
    "trigger_compute_handler",
    "channel_send_handler",
    "invoke_channel_action_handler",
    "webhook_receive_handler",
    "CANONICAL_KINDS",
    "AppendValidationError",
    "AppendNotFoundError",
    "append_to_field",
    "build_route_specs",
    "dispatch_http_request",
]
