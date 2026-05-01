# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Pure handlers for the channel HTTP endpoints.

Slice 7.2.x of Phase 7 (2026-04-30) extracts three routes from
``termin-compiler/termin_runtime/routes.py`` into pure handlers:

* :func:`invoke_channel_action_handler` — ``POST
  /api/v1/channels/{channel_name}/actions/{action_name}``
* :func:`channel_send_handler` — ``POST
  /api/v1/channels/{channel_name}/send``
* :func:`webhook_receive_handler` — per-channel inbound ``POST
  /webhooks/{channel_snake}`` route.

The first two delegate to ``ctx.channel_dispatcher`` for the actual
provider invocation; the handler's job is auth, body-parsing, and
mapping the dispatcher's exception taxonomy onto :class:`TerminScopeError`
/ :class:`TerminValidationError` / :class:`TerminConflictError`.

The third — webhook receive — is the only route that touches storage
directly. It validates the payload against the channel's carried
content schema, persists a record, fires content-event handlers, and
publishes the ``content.<X>.created`` event. All ctx-stash callables
already exposed by the runtime for the CRUD handlers (``ctx.storage``,
``ctx.run_event_handlers_for_content``, ``ctx.publish_content_event``).
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

from ..errors import (
    TerminBadRequestError,
    TerminConflictError,
    TerminNotFoundError,
    TerminScopeError,
    TerminValidationError,
)
from .request import TerminRequest, TerminResponse


def _exc_name(exc: BaseException) -> str:
    """Return the runtime exception's class name. Channel dispatcher
    raises framework-agnostic ChannelScopeError / ChannelValidationError
    / ChannelError shapes the runtime defines next to its dispatcher;
    we identify them by name so the core stays decoupled from the
    runtime module."""
    return type(exc).__name__


def _scopes_from(request: TerminRequest) -> set[str]:
    """Collect the user's effective scopes from the request's
    AuthContext. Slice 7.5b: legacy_user_dict fallback removed —
    request.auth is the single source of truth."""
    if request.auth is None:
        return set()
    return set(request.auth.scopes)


def _parse_json_body(request: TerminRequest) -> dict:
    """Parse the request body as JSON and return a dict. Empty body
    yields ``{}``; non-dict JSON (a list, a literal) yields ``{}`` —
    the channel dispatcher and webhook contract both expect mappings.
    """
    if not request.body:
        return {}
    try:
        body = json.loads(request.body) if isinstance(request.body, (bytes, bytearray)) else request.body
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def invoke_channel_action_handler(
    request: TerminRequest,
    ctx: Any,
) -> TerminResponse:
    """Handle ``POST /api/v1/channels/{channel_name}/actions/{action_name}``.

    The dispatcher raises three runtime-side exception types — they
    map to 403 / 422 / 502 respectively. Anything else propagates and
    becomes a 500 via the runtime's generic handler.
    """
    channel_name = request.path_params.get("channel_name", "")
    action_name = request.path_params.get("action_name", "")

    spec = ctx.channel_dispatcher.get_spec(channel_name)
    if not spec:
        raise TerminNotFoundError(f"Channel '{channel_name}' not found")

    action_spec = ctx.channel_dispatcher.get_action_spec(channel_name, action_name)
    if not action_spec:
        raise TerminNotFoundError(
            f"Action '{action_name}' not found on channel '{channel_name}'",
        )

    body = _parse_json_body(request)
    user_scopes = _scopes_from(request)

    try:
        result = await ctx.channel_dispatcher.channel_invoke(
            channel_name, action_name, body, user_scopes=user_scopes,
        )
        return TerminResponse(status_code=200, json_body=result)
    except Exception as exc:
        kind = _exc_name(exc)
        if kind == "ChannelScopeError":
            raise TerminScopeError(str(exc)) from exc
        if kind == "ChannelValidationError":
            raise TerminValidationError(str(exc)) from exc
        if kind == "ChannelError":
            # 502 Bad Gateway — provider failure. Use Conflict
            # (closest 4xx) since core's exception taxonomy intentionally
            # omits 5xx; the runtime's exception_handler gates the
            # actual status code via Termin*.status_code.
            raise TerminConflictError(str(exc)) from exc
        raise


async def channel_send_handler(
    request: TerminRequest,
    ctx: Any,
) -> TerminResponse:
    """Handle ``POST /api/v1/channels/{channel_name}/send``.

    Same exception-mapping shape as :func:`invoke_channel_action_handler`,
    minus the action lookup — channel-level send is a single dispatcher
    entry point.
    """
    channel_name = request.path_params.get("channel_name", "")
    spec = ctx.channel_dispatcher.get_spec(channel_name)
    if not spec:
        raise TerminNotFoundError(f"Channel '{channel_name}' not found")

    body = _parse_json_body(request)
    user_scopes = _scopes_from(request)

    try:
        result = await ctx.channel_dispatcher.channel_send(
            channel_name, body, user_scopes=user_scopes,
        )
        return TerminResponse(status_code=200, json_body=result)
    except Exception as exc:
        kind = _exc_name(exc)
        if kind == "ChannelScopeError":
            raise TerminScopeError(str(exc)) from exc
        if kind == "ChannelError":
            raise TerminConflictError(str(exc)) from exc
        raise


async def webhook_receive_handler(
    request: TerminRequest,
    ctx: Any,
    *,
    channel_spec: dict,
) -> TerminResponse:
    """Handle ``POST /webhooks/{channel_snake}`` for one inbound channel.

    ``channel_spec`` is the IR's channel definition for this route,
    closed over by the route shell so the per-channel data (display
    name, carries_content, requirements) doesn't have to be looked up
    on every request. Same closure-over-IR pattern the runtime uses
    for the per-channel route registration today.

    Side effects:

    1. Validate the payload against ``channel_spec.carries_content``'s
       known columns; reject with 422 if no recognized field present.
    2. ``ctx.storage.create`` the record.
    3. ``ctx.run_event_handlers_for_content`` for ``"created"``.
    4. Update the dispatcher's ``received`` metric.
    5. ``ctx.publish_content_event`` for the ``content.<X>.created``
       broadcast.
    """
    ch_display = channel_spec["name"]["display"]
    ch_carries = channel_spec.get("carries_content", "")
    if not ch_carries:
        raise TerminBadRequestError(
            f"Channel '{ch_display}' carries no content; webhook ignored",
        )

    user_scopes = _scopes_from(request)
    for req in channel_spec.get("requirements", []) or []:
        if req.get("direction") == "send" and req.get("scope") not in user_scopes:
            raise TerminScopeError(
                f"Scope '{req['scope']}' required to send to channel '{ch_display}'",
            )

    if not request.body:
        raise TerminBadRequestError("Invalid JSON payload")
    try:
        body = json.loads(request.body) if isinstance(request.body, (bytes, bytearray)) else request.body
    except Exception:
        raise TerminBadRequestError("Invalid JSON payload")
    if not isinstance(body, dict):
        raise TerminBadRequestError("Invalid JSON payload")

    schema = ctx.content_lookup.get(ch_carries)
    if not schema:
        raise TerminBadRequestError(f"Content '{ch_carries}' not found")

    # Project to known columns. The webhook contract tolerates extra
    # fields silently (mirrors v0.8 behavior); a record with no
    # recognized columns is rejected as 422 since storing an empty
    # row would silently no-op.
    known_cols: set[str] = set()
    for f in schema.get("fields", []) or []:
        fname = f.get("name", "")
        if isinstance(fname, dict):
            known_cols.add(fname.get("snake", ""))
        else:
            known_cols.add(str(fname))
    record_data = {k: v for k, v in body.items() if k in known_cols}
    if not record_data:
        raise TerminValidationError("No valid fields in payload")

    record = await ctx.storage.create(ch_carries, record_data)

    run_event_handlers = getattr(ctx, "run_event_handlers_for_content", None)
    if run_event_handlers is not None:
        await run_event_handlers(ch_carries, "created", record)

    # Dispatcher metric — best-effort. Not present on alternate
    # runtimes that don't track per-channel inbound counts.
    metrics = getattr(getattr(ctx, "channel_dispatcher", None), "_metrics", None)
    if isinstance(metrics, dict):
        bucket = metrics.setdefault(ch_display, {})
        bucket["received"] = int(bucket.get("received", 0)) + 1

    publish = getattr(ctx, "publish_content_event", None)
    if publish is not None:
        await publish("created", ch_carries, record)

    return TerminResponse(
        status_code=200,
        json_body={
            "ok": True,
            "id": record.get("id"),
            "channel": ch_display,
        },
    )


__all__ = [
    "invoke_channel_action_handler",
    "channel_send_handler",
    "webhook_receive_handler",
]
