# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Pure CRUD route handlers — framework-agnostic implementations
that operate on :class:`TerminRequest` / :class:`TerminResponse`.

Slice 7.2.e of Phase 7 (2026-04-30) extracted handlers from
``termin-compiler/termin_runtime/routes.py`` into this module one
at a time. The first one extracted is :func:`list_content_handler`
— GET /api/v1/{content} — chosen as the proof-of-bridge because
its surface is well-defined and its behavior is exhaustively
covered by the conformance suite's pagination/filter/sort tests.

Adapter helpers in the runtime (``termin_runtime/fastapi_adapter.py``)
bridge between FastAPI's Request/Response types and these handlers.
The handler itself takes only :class:`TerminRequest` + ``ctx``;
it has no awareness of the framework hosting it.
"""

from __future__ import annotations

from typing import Any

from ..errors import (
    TerminBadRequestError,
    TerminScopeError,
)
from ..providers.storage_contract import (
    And,
    Eq,
    OrderBy,
    QueryOptions,
)
from .request import TerminRequest, TerminResponse


# Reserved query-param names — not treated as field filters.
# `offset` was retired in v0.9 along with the keyset-cursor
# pagination contract; the runtime rejects it explicitly so callers
# can't silently get incorrect behavior.
_RESERVED_QUERY_PARAMS = frozenset({"limit", "sort", "cursor"})


async def list_content_handler(
    request: TerminRequest,
    ctx: Any,
) -> TerminResponse:
    """Handle ``GET /api/v1/{content}`` — list records of a content
    type.

    Honors ``?limit``, ``?cursor``, ``?sort=field`` /
    ``?sort=field:asc`` / ``?sort=field:desc``, and arbitrary
    ``?<field>=<value>`` filters that match schema fields.
    Rejects ``?offset=`` with a v0.9-migration message.

    Adapter contract:
        * ``request.path_params["content"]`` — the content_ref (snake
          identifier) the route was registered for.
        * ``request.auth`` — :class:`AuthContext` with the
          principal's scopes for boundary-identity check and
          ownership row-filter resolution.

    Optional kwargs — these come from the route's RouteSpec at
    binding time, not from the request:
        * ``ctx.row_filter_for(content_ref)`` — when set,
          per-route row filter (e.g., ownership) joins onto the
          predicate.

    Raises:
        TerminBadRequestError: ?offset= present, malformed limit /
            sort / filter, unknown sort or filter field.
        TerminScopeError: caller's identity violates the boundary's
            identity scope.
    """
    # ── Boundary identity check ──
    cr = request.path_params.get("content", "")
    auth = request.auth
    user_scopes = list(auth.scopes) if auth else []

    # The boundary identity check is a runtime concern that depends
    # on ctx state. The handler delegates via ctx; ctx exposes the
    # check method. (Slice 7.5 may move this rule into core too.)
    bnd_id_err = _check_boundary_identity(ctx, cr, user_scopes)
    if bnd_id_err:
        raise TerminScopeError(bnd_id_err)

    # ── Schema field set ──
    schema = ctx.content_lookup.get(cr, {})
    schema_fields = {f["name"] for f in schema.get("fields", [])}
    schema_fields.update({"id", "status"})
    for sm in ctx.sm_lookup.get(cr, []):
        schema_fields.add(sm["machine_name"])

    qp = request.query_params

    # ── ?offset= retired ──
    if "offset" in qp:
        raise TerminBadRequestError(
            "?offset= was removed in v0.9. Use ?cursor= with the "
            "next_cursor token from a prior response. Cursors are "
            "opaque; do not parse."
        )

    # ── ?limit ──
    limit_from_url: int | None = None
    if "limit" in qp:
        try:
            limit_from_url = int(qp["limit"])
        except ValueError:
            raise TerminBadRequestError(
                f"limit must be an integer, got {qp['limit']!r}"
            )
        if limit_from_url < 0:
            raise TerminBadRequestError("limit must be non-negative")
        if limit_from_url > 1000:
            raise TerminBadRequestError("limit must not exceed 1000")

    # ── ?sort ──
    order_by_list: list[OrderBy] = []
    if "sort" in qp:
        raw = qp["sort"]
        if ":" in raw:
            sf, sd = raw.split(":", 1)
        else:
            sf, sd = raw, "asc"
        sd_lower = sd.lower()
        if sd_lower not in ("asc", "desc"):
            raise TerminBadRequestError(
                f"sort direction must be 'asc' or 'desc', got {sd!r}"
            )
        if sf not in schema_fields:
            raise TerminBadRequestError(
                f"unknown sort field '{sf}' for {cr}"
            )
        order_by_list.append(OrderBy(field=sf, direction=sd_lower))

    # ── ?<field>=<value> filters ──
    filter_eqs: list = []
    for k, v in qp.items():
        if k in _RESERVED_QUERY_PARAMS:
            continue
        if k not in schema_fields:
            raise TerminBadRequestError(
                f"unknown filter field '{k}' for {cr}"
            )
        filter_eqs.append(Eq(field=k, value=v))

    # ── Ownership row_filter (BRD #3 §3.4 / §3.5) ──
    # The route's RouteSpec carries the row_filter; ctx.row_filter_for
    # surfaces it for the handler. Filters where field == principal id.
    row_filter = getattr(ctx, "row_filter_for", lambda _cr: None)(cr)
    if row_filter and row_filter.get("kind") == "ownership":
        owner_field = row_filter.get("field")
        owner_id = auth.principal.id if auth else ""
        if owner_field and owner_id:
            filter_eqs.append(Eq(field=owner_field, value=owner_id))

    predicate = None
    if len(filter_eqs) == 1:
        predicate = filter_eqs[0]
    elif len(filter_eqs) > 1:
        predicate = And(predicates=tuple(filter_eqs))

    # ── Query the storage provider ──
    effective_limit = limit_from_url if limit_from_url is not None else 1000
    url_cursor = qp.get("cursor")
    options = QueryOptions(
        limit=effective_limit,
        cursor=url_cursor,
        order_by=tuple(order_by_list),
    )
    try:
        page = await ctx.storage.query(cr, predicate, options)
    except ValueError as e:
        raise TerminBadRequestError(str(e))

    records = [dict(r) for r in page.records]

    # ── Redaction (legacy ctx hook for now; pure once slice 7.5 lands) ──
    records = _redact_records_via_ctx(ctx, records, schema, set(user_scopes))
    if cr.startswith("compute_audit_log_"):
        records = await _redact_audit_traces_via_ctx(
            ctx, records, cr, set(user_scopes)
        )

    return TerminResponse(json_body=records)


# ── Adapter-side ctx helpers ─────────────────────────────────────
#
# These are thin shims that read functions off the runtime ctx
# rather than importing them directly. Keeps termin-core
# framework-free; the runtime supplies the implementations.


def _check_boundary_identity(ctx, content_ref: str, user_scopes: list[str]) -> str | None:
    """Delegate to ctx-supplied boundary check. ctx may not have
    one configured (anonymous app); returns None if so."""
    fn = getattr(ctx, "_check_boundary_identity", None)
    if fn is None:
        # The runtime bootstraps this on ctx as a closure over
        # boundary_identity_scopes / boundary_for_content. If it's
        # not present, the app has no boundary identity rule and
        # the check is a no-op.
        return None
    return fn(content_ref, user_scopes)


def _redact_records_via_ctx(ctx, records, schema, scopes):
    """Pure redaction logic lives in
    termin_core.confidentiality.redact_records, but we need ctx.scopes
    in the call. Bridge via ctx-supplied function or import."""
    from ..confidentiality import redact_records
    return redact_records(records, schema, scopes)


async def _redact_audit_traces_via_ctx(ctx, records, content_ref, scopes):
    """Audit-trace redaction is a runtime-internal concern that
    lives in termin_runtime/compute_runner.py. Bridge via ctx
    helper. None means no redaction available; pass through."""
    fn = getattr(ctx, "redact_audit_traces", None)
    if fn is None:
        return records
    return await fn(records, content_ref, scopes)


__all__ = ["list_content_handler"]
