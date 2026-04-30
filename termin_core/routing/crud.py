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
    TerminConflictError,
    TerminNotFoundError,
    TerminRuntimeError,
    TerminScopeError,
)
from ..validation import (
    evaluate_field_defaults,
    strip_unknown_fields,
    validate_dependent_values,
    validate_enum_constraints,
    validate_min_max_constraints,
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


async def get_content_handler(
    request: TerminRequest,
    ctx: Any,
) -> TerminResponse:
    """Handle ``GET /api/v1/{content}/{key}`` — read one record.

    The route is registered with a lookup column (typically ``id``,
    sometimes a unique field like ``sku``). The runtime stashes the
    column on ctx via ``ctx.lookup_column_for(content_ref)``.
    Primary-key reads use ``ctx.storage.read``; alternate-key reads
    use ``ctx.storage.query`` with a single-row limit.

    Adapter contract:
        * ``request.path_params["content"]`` — content_ref
        * ``request.path_params["key"]`` — the lookup-column value
        * ``request.auth`` — caller's AuthContext

    Raises:
        TerminNotFoundError: record doesn't exist, or ownership
            row_filter blocks the read (404 by design — the record
            "doesn't exist" from the principal's perspective per
            BRD §3.4; existence shouldn't leak through the auth
            shape).
    """
    cr = request.path_params.get("content", "")
    key = request.path_params.get("key", "")
    auth = request.auth
    user_scopes = set(auth.scopes) if auth else set()

    lookup_col = getattr(ctx, "lookup_column_for", lambda _cr: "id")(cr)
    if lookup_col == "id":
        record = await ctx.storage.read(cr, key)
    else:
        page = await ctx.storage.query(
            cr,
            Eq(field=lookup_col, value=key),
            QueryOptions(limit=1),
        )
        record = dict(page.records[0]) if page.records else None
    if record is None:
        raise TerminNotFoundError("Not found")

    schema = ctx.content_lookup.get(cr, {})

    # ── Ownership row_filter on GET_ONE ──
    # Per BRD §3.4, the row "doesn't exist" from the principal's
    # perspective when the ownership predicate fails — surface 404
    # (not 403) so existence doesn't leak through the auth shape.
    row_filter = getattr(ctx, "row_filter_for", lambda _cr: None)(cr)
    if row_filter and row_filter.get("kind") == "ownership":
        owner_field = row_filter.get("field")
        owner_id = auth.principal.id if auth else ""
        if owner_field and record.get(owner_field) != owner_id:
            raise TerminNotFoundError("Not found")

    # ── Redaction ──
    from ..confidentiality import redact_record
    record = redact_record(record, schema, user_scopes)

    if cr.startswith("compute_audit_log_"):
        redact_audit = getattr(ctx, "redact_audit_traces", None)
        if redact_audit is not None:
            records = await redact_audit([record], cr, user_scopes)
            record = records[0] if records else record

    return TerminResponse(json_body=record)


async def create_content_handler(
    request: TerminRequest,
    ctx: Any,
) -> TerminResponse:
    """Handle ``POST /api/v1/{content}`` — create a record.

    Adapter contract:
        * ``request.path_params["content"]`` — content_ref
        * ``request.headers["content-type"]`` — drives JSON vs form
          parsing.
        * ``request.body`` — raw bytes; the handler parses as JSON
          when content-type contains ``application/json``, else as
          URL-encoded form.
        * ``request.auth`` — caller's AuthContext.

    Per BRD §3.4 / §3.5 ownership: when the route's ``owner_field``
    is set on ctx (``ctx.owner_field_for(content_ref)``), the
    handler stamps that field with the principal's id at create
    time. Overwrites any client-supplied value so apps cannot
    create rows owned by other principals.

    Per BRD multi-state-machine create gate: state-machine columns
    are stripped before validation+insert so the SQL DEFAULT
    applies the machine's initial state. A client-supplied value
    for a state column would otherwise let a caller bootstrap a
    record already past its initial state, bypassing transition
    rules.

    Raises:
        TerminScopeError: caller violates the boundary's identity scope.
        TerminBadRequestError: NOT NULL constraint violation,
            malformed body.
        TerminConflictError: UNIQUE constraint violation
            (idempotency-key collision, etc.).
        TerminValidationError: D-19 dependent_values / one_of /
            min/max bound violation.
        TerminRuntimeError: any other persistence error (mapped to
            500 by the HTTP adapter).
    """
    cr = request.path_params.get("content", "")
    auth = request.auth
    user_scopes = list(auth.scopes) if auth else []

    bnd_id_err = _check_boundary_identity(ctx, cr, user_scopes)
    if bnd_id_err:
        raise TerminScopeError(bnd_id_err)

    # ── Body parsing — JSON or form ──
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json() or {}
    else:
        form = await request.form()
        body = {k: v for k, v in form.items() if v}

    # ── State-machine column gate ──
    sm_info = getattr(ctx, "state_machine_info_for", lambda _cr: None)(cr)
    seed_state = getattr(ctx, "seed_state_columns", None)
    if seed_state is not None:
        body = seed_state(body, sm_info, strip_existing=True)

    # ── Ownership stamping ──
    owner_field = getattr(ctx, "owner_field_for", lambda _cr: None)(cr)
    if owner_field:
        owner_id = auth.principal.id if auth else ""
        body[owner_field] = owner_id

    schema = ctx.content_lookup.get(cr, {})

    # ── Defaults + validation (validation raises TerminValidationError) ──
    # evaluate_field_defaults reads ``user["User"]`` for the
    # PascalCase-keyed CEL shape (User.Username, User.Role, etc.)
    # IR-declared default_expr expressions reference. The adapter
    # middleware stamps the legacy user dict on request for this
    # purpose during the slice-7.2.e migration; slice 7.5 ports
    # the shape into AuthContext or rewrites the CEL surface.
    user_dict_for_defaults = request.legacy_user_dict or {}
    evaluate_field_defaults(body, schema, ctx.expr_eval, user_dict_for_defaults)
    validate_enum_constraints(body, schema)
    validate_min_max_constraints(body, schema)
    validate_dependent_values(cr, body, ctx.content_lookup, ctx.expr_eval)
    body = strip_unknown_fields(body, schema)

    # ── Persist ──
    try:
        record = await ctx.storage.create(cr, body)
        if seed_state is not None:
            record = seed_state(dict(record), sm_info)
        publish = getattr(ctx, "publish_content_event", None)
        if publish is not None:
            await publish("created", cr, record)
    except TerminRuntimeError:
        # Re-raise framework-agnostic exceptions unchanged.
        raise
    except Exception as e:
        # Route through TerminAtor for observability.
        terminator_route = getattr(ctx, "route_terminator_validation", None)
        if terminator_route is not None:
            terminator_route(cr, e)
        err_msg = str(e)
        if "UNIQUE constraint" in err_msg:
            raise TerminConflictError(err_msg)
        if "NOT NULL constraint" in err_msg:
            raise TerminBadRequestError(err_msg)
        raise TerminRuntimeError(err_msg)

    # ── Run IR-declared event handlers ──
    run_event_handlers = getattr(ctx, "run_event_handlers_for_content", None)
    if run_event_handlers is not None:
        await run_event_handlers(cr, "created", record)

    # ── Redact + return ──
    from ..confidentiality import redact_record
    record = redact_record(record, schema, set(user_scopes))
    return TerminResponse(status_code=201, json_body=record)


def _user_dict_for_state_transition(auth) -> dict:
    """Project an :class:`AuthContext` into the user-dict shape
    :func:`termin_core.state.machine.do_state_transition` expects.

    Transitional helper — slice 7.5 ports do_state_transition to
    take an AuthContext directly, at which point this projection
    goes away.
    """
    if auth is None:
        return {"scopes": [], "the_user": None}
    return {
        "scopes": list(auth.scopes),
        "role": auth.role_name,
        "the_user": {
            "id": auth.principal.id,
            "display_name": auth.principal.display_name,
            "type": auth.principal.type,
            "is_anonymous": auth.is_anonymous,
            "is_system": auth.is_system,
            "scopes": list(auth.scopes),
        },
    }


async def update_content_handler(
    request: TerminRequest,
    ctx: Any,
) -> TerminResponse:
    """Handle ``PUT /api/v1/{content}/{key}`` — update one record.

    Adapter contract:
        * ``request.path_params["content"]`` — content_ref
        * ``request.path_params["key"]`` — lookup-column value
        * ``request.body`` — JSON body with the fields to update.
        * ``request.auth`` — caller's AuthContext.

    Per BRD §3.4 / §3.5 ownership: when the route's ``row_filter``
    is ownership, a non-owner sees 404 (the row "doesn't exist"
    from their perspective; existence shouldn't leak through the
    auth shape). Attempts to overwrite the ownership field are
    silently stripped from the body — preserves the original
    owner.

    Per BRD multi-state-machine PUT gate: any state-column value
    in the body that differs from the current record routes
    through :func:`do_state_transition` so the declared transition
    table + required scope are honored. Multiple touched machines
    transition in IR order; any failure stops the chain (and the
    state-machine engine surfaces the failure as a Termin*Error).

    Raises:
        TerminScopeError: write-access check fails (write to a
            confidentiality-scoped field without the scope).
        TerminNotFoundError: record doesn't exist, or ownership
            row_filter blocks the access.
    """
    cr = request.path_params.get("content", "")
    key = request.path_params.get("key", "")
    auth = request.auth
    user_scopes = set(auth.scopes) if auth else set()

    body = await request.json() or {}
    schema = ctx.content_lookup.get(cr, {})

    # ── Write-access check ──
    from ..confidentiality import check_write_access
    write_err = check_write_access(body, schema, user_scopes)
    if write_err:
        raise TerminScopeError(write_err)

    # ── Resolve target record by lookup column ──
    lookup_col = getattr(ctx, "lookup_column_for", lambda _cr: "id")(cr)
    if lookup_col == "id":
        existing = await ctx.storage.read(cr, key)
        target_id = key
    else:
        page = await ctx.storage.query(
            cr,
            Eq(field=lookup_col, value=key),
            QueryOptions(limit=1),
        )
        existing = dict(page.records[0]) if page.records else None
        target_id = existing["id"] if existing else None

    # ── Ownership row_filter ──
    row_filter = getattr(ctx, "row_filter_for", lambda _cr: None)(cr)
    if row_filter and row_filter.get("kind") == "ownership":
        owner_field = row_filter.get("field")
        owner_id = auth.principal.id if auth else ""
        if existing and owner_field and existing.get(owner_field) != owner_id:
            raise TerminNotFoundError("Not found")
        # Strip ownership-field overwrites silently.
        if owner_field in body:
            body = {k: v for k, v in body.items() if k != owner_field}

    # ── State-machine PUT gate ──
    if existing and cr in ctx.sm_lookup:
        from ..state import do_state_transition

        sm_list = ctx.sm_lookup.get(cr, [])
        state_cols = {sm["machine_name"] for sm in sm_list}
        touched = [sm for sm in sm_list if sm["machine_name"] in body]
        if touched:
            user_dict = _user_dict_for_state_transition(auth)
            for sm in touched:
                col = sm["machine_name"]
                new_val = body.get(col, "")
                cur_val = existing.get(col, "")
                if new_val != cur_val:
                    await do_state_transition(
                        ctx.storage, cr, existing["id"], col, new_val,
                        user_dict, ctx.sm_lookup,
                        ctx.terminator, ctx.event_bus,
                    )
        if state_cols:
            body = {k: v for k, v in body.items() if k not in state_cols}

    # ── Validate (merged view if updating) ──
    if existing:
        merged = dict(existing)
        merged.update(body)
        validate_dependent_values(cr, merged, ctx.content_lookup, ctx.expr_eval)
    else:
        validate_dependent_values(cr, body, ctx.content_lookup, ctx.expr_eval)

    # ── Persist or read-back ──
    if body and target_id is not None:
        try:
            record = await ctx.storage.update(cr, target_id, body)
        except TerminRuntimeError:
            raise
        except Exception as e:
            terminator_route = getattr(ctx, "route_terminator_validation", None)
            if terminator_route is not None:
                terminator_route(cr, e)
            raise
        if record is None:
            raise TerminNotFoundError("Not found")
        record = dict(record)
        publish = getattr(ctx, "publish_content_event", None)
        if publish is not None:
            await publish("updated", cr, record)
        run_event_handlers = getattr(ctx, "run_event_handlers_for_content", None)
        if run_event_handlers is not None:
            await run_event_handlers(cr, "updated", record)
    else:
        # Body was state-only and applied via transition, OR no
        # record to update.
        if target_id is None:
            raise TerminNotFoundError("Not found")
        record = await ctx.storage.read(cr, target_id)
        if record is None:
            raise TerminNotFoundError("Not found")

    from ..confidentiality import redact_record
    record = redact_record(record, schema, user_scopes)
    return TerminResponse(json_body=record)


async def delete_content_handler(
    request: TerminRequest,
    ctx: Any,
) -> TerminResponse:
    """Handle ``DELETE /api/v1/{content}/{key}`` — delete one record.

    Adapter contract:
        * ``request.path_params["content"]`` — content_ref
        * ``request.path_params["key"]`` — lookup-column value
        * ``request.auth`` — caller's AuthContext.

    Cascade semantics: the route passes ``CascadeMode.RESTRICT`` as
    the caller's *intent*. Actual cascade behavior comes from each
    child's FK declaration in the schema (ON DELETE CASCADE vs
    ON DELETE RESTRICT, emitted from the IR's
    ``FieldSpec.cascade_mode``). The reference SQLite provider
    treats RESTRICT as "if any FK violation, raise"; future
    providers may consult the arg differently.

    Raises:
        TerminNotFoundError: record doesn't exist, or ownership
            row_filter blocks the delete (404 by design — see
            get_content_handler for rationale).
        TerminConflictError: foreign-key violation (other records
            reference this one).
    """
    from ..providers.storage_contract import CascadeMode

    cr = request.path_params.get("content", "")
    key = request.path_params.get("key", "")
    auth = request.auth

    # ── Resolve target id ──
    lookup_col = getattr(ctx, "lookup_column_for", lambda _cr: "id")(cr)
    target_id = key
    if lookup_col != "id":
        page = await ctx.storage.query(
            cr,
            Eq(field=lookup_col, value=key),
            QueryOptions(limit=1),
        )
        if not page.records:
            raise TerminNotFoundError("Record not found")
        target_id = page.records[0].get("id")

    # ── Ownership row_filter on DELETE ──
    row_filter = getattr(ctx, "row_filter_for", lambda _cr: None)(cr)
    if row_filter and row_filter.get("kind") == "ownership":
        owner_field = row_filter.get("field")
        owner_id = auth.principal.id if auth else ""
        if owner_field:
            rec = await ctx.storage.read(cr, target_id)
            if rec is None or rec.get(owner_field) != owner_id:
                raise TerminNotFoundError("Record not found")

    # ── Delete ──
    try:
        deleted = await ctx.storage.delete(
            cr, target_id, cascade_mode=CascadeMode.RESTRICT,
        )
    except TerminRuntimeError:
        raise
    except Exception as e:
        msg = str(e)
        # FK violation translates to 409 with a friendly message.
        if "FOREIGN KEY" in msg.upper():
            singular = cr[:-1] if cr.endswith("s") else cr
            detail = (
                f"Cannot delete this {singular}: other records "
                f"reference it. Remove or reassign those first."
            )
            terminator_route = getattr(ctx, "route_terminator_validation", None)
            if terminator_route is not None:
                terminator_route(cr, e)
            raise TerminConflictError(detail)
        raise

    if not deleted:
        raise TerminNotFoundError("Record not found")

    publish = getattr(ctx, "publish_content_event", None)
    if publish is not None:
        await publish("deleted", cr, {"id": target_id})

    return TerminResponse(json_body={"deleted": True})


async def transition_content_handler(
    request: TerminRequest,
    ctx: Any,
) -> TerminResponse:
    """Handle ``POST /api/v1/{content}/{key}/_transition/{machine}/{target}``
    — declared state transition.

    Adapter contract:
        * ``request.path_params["content"]`` — content_ref
        * ``request.path_params["key"]`` — lookup-column value
        * ``request.path_params["machine"]`` — state machine name
          (snake_case). Falls back to the first state machine on the
          content if missing — back-compat for legacy IRs without
          machine_name in the RouteSpec.
        * ``request.path_params["target"]`` — target state, with
          underscores converted to spaces (``in_progress`` →
          ``"in progress"``).
        * ``request.auth`` — caller's AuthContext.

    Delegates to :func:`termin_core.state.machine.do_state_transition`,
    which raises Termin*Error on declared-transition / scope /
    not-found / concurrent-CAS-failure conditions.
    """
    from ..state import do_state_transition

    cr = request.path_params.get("content", "")
    key = request.path_params.get("key", "")
    machine = request.path_params.get("machine") or None
    target_state = request.path_params.get("target", "")
    auth = request.auth

    # ── Resolve target row by lookup column ──
    lookup_col = getattr(ctx, "lookup_column_for", lambda _cr: "id")(cr)
    if lookup_col == "id":
        row = await ctx.storage.read(cr, key)
    else:
        page = await ctx.storage.query(
            cr,
            Eq(field=lookup_col, value=key),
            QueryOptions(limit=1),
        )
        row = dict(page.records[0]) if page.records else None
    if not row:
        raise TerminNotFoundError("Not found")

    # ── Resolve machine_name with back-compat fallback ──
    if machine is None:
        sms = ctx.sm_lookup.get(cr, [])
        if sms:
            machine = sms[0]["machine_name"]
        else:
            raise TerminBadRequestError(f"No state machine for {cr}")

    user_dict = _user_dict_for_state_transition(auth)
    record = await do_state_transition(
        ctx.storage, cr, row["id"], machine, target_state,
        user_dict, ctx.sm_lookup,
        ctx.terminator, ctx.event_bus,
    )
    return TerminResponse(json_body=record)


__all__ = [
    "list_content_handler",
    "get_content_handler",
    "create_content_handler",
    "update_content_handler",
    "delete_content_handler",
    "transition_content_handler",
]
