# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Pure handler for the manual compute trigger endpoint.

Slice 7.2.x of Phase 7 (2026-04-30) extracts the
``POST /api/v1/compute/{compute_name}/trigger`` route from
``termin-compiler/termin_runtime/compute_runner.py``. The endpoint lets
operators (or test harnesses) re-fire any Compute on a specific
record regardless of declared trigger type — used for re-runs,
dev-loop iteration, and edge-case debugging of llm/ai-agent computes
whose normal trigger is event- or schedule-driven.

The handler depends on three ctx hooks the runtime already supplies:

* ``ctx.compute_lookup`` — snake compute name → compute IR dict.
* ``ctx.content_lookup`` — snake content name → content schema (for
  validating the supplied ``content_name``).
* ``ctx.execute_compute`` — async ``(comp, record, content_name,
  main_loop) -> Any`` that runs the compute. Same dispatch surface
  the event bus and trigger filters use.
* ``ctx.terminator`` — error router; receives confidentiality-gate
  rejections so the audit trail captures them. Optional; falls back
  to no-op when absent.
* ``ctx.check_compute_access`` — ``(comp, user_scopes) -> str | None``
  confidentiality gate; returns a reason string when the principal
  lacks access. Optional — when absent, the gate is permissive (some
  alternate runtimes may not implement field confidentiality).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from ..errors import (
    TerminBadRequestError,
    TerminError,
    TerminNotFoundError,
    TerminScopeError,
)
from .request import TerminRequest, TerminResponse


async def trigger_compute_handler(
    request: TerminRequest,
    ctx: Any,
) -> TerminResponse:
    """Handle ``POST /api/v1/compute/{compute_name}/trigger``.

    Scope, confidentiality gate, content_name resolution, and the
    actual ``execute_compute`` dispatch all happen here. The bridge in
    the runtime is the standard ten-line FastAPI shell.

    Path params: ``compute_name`` — the snake-case compute identifier.
    Body shape::

        {"record": {...}, "content_name": "<snake>"}

    ``record`` is the input the compute would otherwise receive from
    the triggering event. ``content_name`` is required when the
    compute declares multiple input content types; inferred otherwise.
    """
    compute_name = request.path_params.get("compute_name", "")
    comp = ctx.compute_lookup.get(compute_name)
    if not comp:
        raise TerminNotFoundError(f"Compute '{compute_name}' not found")

    auth = request.auth
    user_scopes = set(auth.scopes) if auth else set()

    req_scope = comp.get("required_scope")
    if req_scope and req_scope not in user_scopes:
        raise TerminScopeError(
            f"Requires scope '{req_scope}' to trigger",
        )

    check = getattr(ctx, "check_compute_access", None)
    if check is not None:
        gate_err = check(comp, user_scopes)
        if gate_err:
            terminator = getattr(ctx, "terminator", None)
            if terminator is not None:
                # Terminator instrumentation is best-effort; the
                # primary contract is rejecting the call below.
                try:
                    terminator.route(TerminError(
                        source=comp["name"]["display"],
                        kind="confidentiality_gate_rejected",
                        message=gate_err,
                    ))
                except Exception:
                    pass
            raise TerminScopeError(gate_err)

    if request.body:
        try:
            body = json.loads(request.body) if isinstance(request.body, (bytes, bytearray)) else request.body
        except Exception:
            raise TerminBadRequestError("Request body must be JSON")
        if not isinstance(body, dict):
            body = {}
    else:
        body = {}

    record = body.get("record", {}) or {}
    content_name = body.get("content_name") or ""

    # If caller didn't specify content_name, try to infer from the
    # compute's declared input content. Multiple inputs without an
    # explicit name is ambiguous and rejected.
    if not content_name:
        input_content = comp.get("input_content", []) or []
        if len(input_content) == 1:
            content_name = input_content[0]
        elif len(input_content) == 0:
            content_name = ""
        else:
            raise TerminBadRequestError(
                f"Compute '{compute_name}' has multiple input content types; "
                "specify 'content_name' in the request body"
            )

    if content_name and content_name not in ctx.content_lookup:
        raise TerminBadRequestError(f"Unknown content_name '{content_name}'")

    invocation_id = str(uuid.uuid4())
    try:
        main_loop = asyncio.get_running_loop()
    except RuntimeError:
        main_loop = None

    # v0.9.1: pass the upstream principal so the audit row stamps
    # invoked_by_principal_id correctly (anonymous callers get a
    # synthesized "anonymous:<id>" marker per BRD §6.3.4 audit
    # trail requirements).
    invoked_by = getattr(request.auth, "principal", None) if request.auth else None
    await ctx.execute_compute(
        comp, record, content_name, main_loop=main_loop,
        invoked_by=invoked_by,
    )

    return TerminResponse(
        status_code=200,
        json_body={
            "invocation_id": invocation_id,
            "compute": comp["name"]["display"],
            "provider": comp.get("provider", "cel"),
            "trigger": "manual",
            "status": "completed",
        },
    )


__all__ = ["trigger_compute_handler"]
