# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""HTTP route specification builder + dispatcher (v0.9.3 issue #2).

Two surfaces, both framework-agnostic, both consuming the same
underlying per-class handlers (``list_content_handler``,
``create_content_handler``, ``invoke_channel_action_handler``, etc.):

1. :func:`build_route_specs` — walks the IR and produces a complete
   ``list[RouteSpec]`` + ``list[WebSocketRouteSpec]``. Adapters
   that want to register routes per-route (FastAPI, Starlette,
   ASGI raw) iterate this list.

2. :func:`dispatch_http_request` — convenience function that takes
   a :class:`TerminRequest`, matches its method+path against the
   route specs, and dispatches to the right handler. Adapters
   that prefer single-entry-point dispatch (an ASGI catch-all,
   or a custom request-loop) call this.

The per-class handlers (``crud.list_content_handler``,
``append.append_to_field``, ``channels.channel_send_handler``,
etc.) are the source of truth for the actual dispatch logic.
This module is just a thin router on top.

Reflection routes (``/api/reflect/*``) and runtime-asset routes
(``/runtime/termin.js``, ``/runtime/termin.css``,
``/runtime/registry``, ``/runtime/bootstrap``) are NOT included
in the route-spec build — they're either trivial dispatches to
``ctx.reflection.X()`` (alt runtimes can wire those directly) or
they serve framework-specific assets (Tailwind CDN bootstrap,
``termin.js``) that don't make sense outside the reference
runtime.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from .append import append_to_field, AppendValidationError, AppendNotFoundError
from .channels import (
    channel_send_handler, invoke_channel_action_handler,
    webhook_receive_handler,
)
from .crud import (
    create_content_handler, delete_content_handler, get_content_handler,
    list_content_handler, transition_content_handler, update_content_handler,
)
from .request import TerminRequest, TerminResponse
from .route_specs import RouteSpec


# ── Route-kind → handler mapping ──

_HANDLER_FOR_KIND = {
    "LIST": list_content_handler,
    "CREATE": create_content_handler,
    "GET_ONE": get_content_handler,
    "UPDATE": update_content_handler,
    "DELETE": delete_content_handler,
    "TRANSITION": transition_content_handler,
}


def _make_crud_handler(handler_fn, content_ref: str):
    """Wrap a per-class handler so it carries the content_ref into
    the request's path_params, then delegates to the bare handler.

    The core handlers expect ``request.path_params['content']`` to
    name the content type. The IR pre-computes the path with the
    content slug baked in (``/api/v1/products``); the dispatcher
    has to inject ``content`` into ``path_params`` so the handler
    knows what to operate on without re-parsing the path.
    """
    async def _wrapped(request: TerminRequest, ctx: Any) -> TerminResponse:
        new_pp = dict(request.path_params or {})
        new_pp["content"] = content_ref
        request_with_content = dataclasses.replace(request, path_params=new_pp)
        return await handler_fn(request_with_content, ctx)
    _wrapped.__name__ = f"{handler_fn.__name__}__{content_ref}"
    return _wrapped


def _make_append_handler(content_ref: str, field_name: str, row_filter):
    """Wrap append_to_field as an HttpHandler.

    Translates the ``AppendValidationError``/``AppendNotFoundError``
    exceptions to ``TerminResponse`` with the appropriate status
    code. The reference runtime's FastAPI adapter could choose to
    re-raise as ``HTTPException`` instead; this wrapper picks the
    transport-neutral path so dispatchers that don't have framework
    exception classes (raw ASGI) get a clean response back.
    """
    async def _wrapped(request: TerminRequest, ctx: Any) -> TerminResponse:
        # Path param: {id} is the parent record's primary key.
        path_params = request.path_params or {}
        key_val = path_params.get("id")

        try:
            payload = await request.json()
        except Exception:
            return TerminResponse(
                status_code=400,
                json_body={"detail": "Invalid JSON body"},
            )

        # Pull principal off the auth context.
        user = None
        auth = getattr(request, "auth", None)
        if auth is not None:
            principal = getattr(auth, "principal", None)
            if principal is not None:
                user = (
                    principal if isinstance(principal, dict)
                    else getattr(principal, "__dict__", None)
                )

        try:
            entry = await append_to_field(
                ctx,
                content_ref=content_ref,
                key_val=key_val,
                field_name=field_name,
                payload=payload,
                user=user,
                row_filter=row_filter,
            )
        except AppendValidationError as e:
            return TerminResponse(
                status_code=400,
                json_body={"detail": e.message},
            )
        except AppendNotFoundError as e:
            return TerminResponse(
                status_code=404,
                json_body={"detail": e.message},
            )

        return TerminResponse(status_code=201, json_body=entry)
    _wrapped.__name__ = f"append_handler__{content_ref}__{field_name}"
    return _wrapped


# ── Builder ──

def build_route_specs(ctx) -> list[RouteSpec]:
    """Walk the IR's pre-computed routes and produce RouteSpecs.

    Args:
        ctx: RuntimeContext with ``.ir`` (dict) carrying the compiled
            AppSpec. The IR's ``routes`` array is the source of
            truth for what routes the app exposes.

    Returns:
        A list of :class:`RouteSpec`, one per IR route entry that
        maps to a known handler kind. Channel routes (send, invoke,
        webhook) are added on top by walking ``ir.channels``.
        Reflection and runtime-asset routes are NOT included
        (alt runtimes wire those directly to ``ctx.reflection``).
    """
    ir = ctx.ir
    specs: list[RouteSpec] = []

    # CRUD + transition + append routes from ir.routes.
    for route in ir.get("routes", []):
        kind = route.get("kind", "")
        method = route.get("method", "")
        path = route.get("path", "")
        scope = route.get("required_scope")
        content_ref = route.get("content_ref", "")

        if kind == "APPEND":
            field_name = route.get("field_name", "")
            row_filter = route.get("row_filter")
            specs.append(RouteSpec(
                method=method,
                path=path,
                handler=_make_append_handler(
                    content_ref, field_name, row_filter,
                ),
                required_scope=scope,
                description=f"Append to {content_ref}.{field_name}",
                tags=("append", content_ref),
            ))
            continue

        handler_fn = _HANDLER_FOR_KIND.get(kind)
        if handler_fn is None:
            continue

        specs.append(RouteSpec(
            method=method,
            path=path,
            handler=_make_crud_handler(handler_fn, content_ref),
            required_scope=scope,
            description=f"{kind} {content_ref}",
            tags=(kind.lower(), content_ref),
        ))

    # Channel routes from ir.channels.
    for channel in ir.get("channels", []):
        ch_display = channel.get("name", {}).get("display", "")
        ch_snake = channel.get("name", {}).get("snake", ch_display.lower())
        if not ch_snake:
            continue

        # Channel send: POST /api/v1/_channels/<channel>/send
        specs.append(RouteSpec(
            method="POST",
            path=f"/api/v1/_channels/{ch_snake}/send",
            handler=channel_send_handler,
            description=f"Send via channel {ch_display}",
            tags=("channel-send", ch_snake),
        ))

        # Channel actions (invoke).
        for action in channel.get("actions", []):
            act_display = action.get("name", {}).get("display", "")
            act_snake = action.get("name", {}).get("snake", act_display.lower())
            if not act_snake:
                continue
            specs.append(RouteSpec(
                method="POST",
                path=f"/api/v1/_channels/{ch_snake}/{act_snake}",
                handler=invoke_channel_action_handler,
                description=f"Invoke {ch_display}.{act_display}",
                tags=("channel-invoke", ch_snake, act_snake),
            ))

        # Inbound webhook (if direction supports inbound).
        direction = channel.get("direction", "")
        if direction in ("inbound", "bidirectional"):
            specs.append(RouteSpec(
                method="POST",
                path=f"/api/v1/_channels/{ch_snake}/webhook",
                handler=webhook_receive_handler,
                description=f"Webhook receiver for {ch_display}",
                tags=("channel-webhook", ch_snake),
            ))

    return specs


# ── Dispatcher ──

# Translate {param} placeholders to a regex named-group pattern.
_PARAM_RE = re.compile(r"\{([^}]+)\}")


def _path_to_regex(path: str) -> re.Pattern:
    """Convert a route path with ``{param}`` placeholders to a
    compiled regex. Used by ``dispatch_http_request`` to match
    incoming request paths against the route table.
    """
    pattern = _PARAM_RE.sub(r"(?P<\1>[^/]+)", path)
    return re.compile(f"^{pattern}$")


async def dispatch_http_request(ctx, request: TerminRequest) -> TerminResponse:
    """Convenience: route an incoming request to the right handler.

    Builds the route specs (cached on ctx after first call), matches
    ``request.method`` + ``request.path`` against the route table,
    enforces per-route ``required_scope`` against ``request.auth``,
    and invokes the matched handler.

    Response codes:

      * **404** — no route matches the path
      * **405** — a route matches the path but not the method
      * **403** — a route matches but the caller's auth doesn't carry
        the route's ``required_scope`` (or the route has a
        ``required_scope`` and the request has no ``auth`` at all)
      * Whatever the handler returns otherwise

    Status precedence is ``404 > 405 > 403 > handler`` — scope is
    only checked once a route has matched on both path and method,
    so an unauthenticated probe of a non-existent path returns 404,
    not 403.

    This is the two-layer security model in action: per-route
    ``required_scope`` is Layer 1 (the dispatcher enforces it here);
    per-content boundary identity is Layer 2 (the CRUD handlers
    enforce it via ``ctx._check_boundary_identity``). See
    ``termin-runtime-implementers-guide.md`` §3 for the model and
    §6 for the dispatcher contract.

    Adapters that bypass ``dispatch_http_request`` and iterate
    ``build_route_specs(ctx)`` directly are responsible for
    enforcing ``RouteSpec.required_scope`` themselves before calling
    the handler. The pattern is::

        for spec in build_route_specs(ctx):
            adapter.add_route(
                method=spec.method, path=spec.path,
                handler=_with_scope_check(spec, ctx),
            )

    Closes (1) of issue #6 — the dispatcher used to silently bypass
    ``required_scope``, leaving alt-runtime adopters without
    per-route scope enforcement.
    """
    # Lazy build + cache.
    cache = getattr(ctx, "_route_specs_cache", None)
    if cache is None:
        specs = build_route_specs(ctx)
        regexes = [(spec, _path_to_regex(spec.path)) for spec in specs]
        ctx._route_specs_cache = (specs, regexes)
        cache = ctx._route_specs_cache
    _specs, regexes = cache

    method = request.method.upper()
    path = request.path

    method_mismatch = False
    for spec, regex in regexes:
        m = regex.match(path)
        if not m:
            continue
        if spec.method != method:
            method_mismatch = True
            continue

        # Scope gate. Layer 1 of the two-layer security model:
        # per-route scope check, run before any handler logic.
        # Adapters routing through dispatch_http_request inherit
        # the enforcement automatically.
        if spec.required_scope is not None:
            auth = getattr(request, "auth", None)
            if auth is None or not auth.has_scope(spec.required_scope):
                return TerminResponse(
                    status_code=403,
                    json_body={
                        "detail": (
                            f"Forbidden: route requires scope "
                            f"{spec.required_scope!r}"
                        ),
                    },
                )

        # Match found: inject path params and dispatch.
        new_pp = dict(request.path_params or {})
        new_pp.update(m.groupdict())
        req = dataclasses.replace(request, path_params=new_pp)
        return await spec.handler(req, ctx)

    status = 405 if method_mismatch else 404
    detail = (
        "Method not allowed for this path"
        if method_mismatch else "No route matches"
    )
    return TerminResponse(status_code=status, json_body={"detail": detail})


__all__ = [
    "build_route_specs",
    "dispatch_http_request",
]
