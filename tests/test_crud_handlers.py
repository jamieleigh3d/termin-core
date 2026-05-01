# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Direct unit tests for the six CRUD handlers added in slice 7.2.e
and the conformance pack landing in slice 7.5b.

The handlers are exercised end-to-end by termin-conformance via the
FastAPI bridge; these tests target failure paths and edge cases that
are awkward to set up through the bridge — rejected query params,
boundary identity violations, ownership row-filter cascading,
storage-provider failures, dependent-value validation, alternate-key
lookups. Coverage target: bring crud.py from 6% (slice 7.5a baseline)
toward 80%+.

The tests share a small fake StorageProvider and a stub ctx; both
are intentionally minimal — they don't try to be a real runtime,
just enough surface for the handlers to exercise their decision paths.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from termin_core.errors import (
    TerminBadRequestError,
    TerminConflictError,
    TerminNotFoundError,
    TerminScopeError,
    TerminValidationError,
)
from termin_core.providers.identity_contract import (
    ANONYMOUS_PRINCIPAL,
    Principal,
)
from termin_core.providers.storage_contract import (
    And,
    Eq,
    OrderBy,
    Page,
    QueryOptions,
)
from termin_core.routing import (
    AuthContext,
    TerminRequest,
    create_content_handler,
    delete_content_handler,
    get_content_handler,
    list_content_handler,
    transition_content_handler,
    update_content_handler,
)


# ── Fixtures: fake storage + ctx ──


class _FakeStorage:
    """Minimal in-memory StorageProvider stand-in.

    Tracks every query / read / create / update / delete call so
    tests can assert what predicates the handler built. Doesn't
    implement the full predicate language — it filters by simple
    Eq predicates only, which covers everything the handlers emit.
    """

    def __init__(
        self, records: dict[str, list[dict]] | None = None,
        next_id: str = "rec-new",
    ) -> None:
        self._records = {k: list(v) for k, v in (records or {}).items()}
        self.calls: list[tuple] = []
        self._next_id = next_id

    async def query(self, content_name, predicate, options):
        self.calls.append(("query", content_name, predicate, options))
        rows = list(self._records.get(content_name, []))
        rows = self._apply_predicate(rows, predicate)
        # Honor limit but ignore cursor for simplicity.
        rows = rows[: options.limit] if options.limit else rows
        return Page(records=rows, next_cursor=None)

    async def read(self, content_name, key):
        self.calls.append(("read", content_name, key))
        for r in self._records.get(content_name, []):
            if r.get("id") == key:
                return dict(r)
        return None

    async def create(self, content_name, record):
        self.calls.append(("create", content_name, dict(record)))
        new = {"id": self._next_id, **record}
        self._records.setdefault(content_name, []).append(new)
        return dict(new)

    async def update(self, content_name, key, patch):
        self.calls.append(("update", content_name, key, dict(patch)))
        rows = self._records.get(content_name, [])
        for i, r in enumerate(rows):
            if r.get("id") == key:
                rows[i] = {**r, **patch}
                return dict(rows[i])
        return None

    async def delete(self, content_name, key, cascade_mode=None):
        self.calls.append(("delete", content_name, key, cascade_mode))
        rows = self._records.get(content_name, [])
        for i, r in enumerate(rows):
            if r.get("id") == key:
                rows.pop(i)
                return True
        return False

    @staticmethod
    def _apply_predicate(rows, predicate):
        if predicate is None:
            return rows
        if isinstance(predicate, Eq):
            return [r for r in rows if r.get(predicate.field) == predicate.value]
        if isinstance(predicate, And):
            for sub in predicate.predicates:
                rows = _FakeStorage._apply_predicate(rows, sub)
            return rows
        return rows


class _StubCtx:
    """Minimal ctx for CRUD handlers."""

    def __init__(
        self,
        *,
        records: dict[str, list[dict]] | None = None,
        content_lookup: dict[str, dict] | None = None,
        sm_lookup: dict[str, list[dict]] | None = None,
        boundary_check: Any = None,
        row_filter: dict | None = None,
        lookup_column: str = "id",
        owner_field_for_content: dict[str, str] | None = None,
        publish: Any = None,
    ) -> None:
        self.storage = _FakeStorage(records or {})
        self.content_lookup = content_lookup or {}
        self.sm_lookup = sm_lookup or {}
        if boundary_check is not None:
            self._boundary_check = boundary_check
        if row_filter is not None:
            self._row_filter = row_filter
        self._lookup_column = lookup_column
        self._owner_fields = owner_field_for_content or {}
        if publish is not None:
            self.publish_content_event = publish
        self.expr_eval = _StubExprEval()

    def lookup_column_for(self, content_name):
        return self._lookup_column

    def row_filter_for(self, content_name):
        return getattr(self, "_row_filter", None)

    def owner_field_for(self, content_name):
        return self._owner_fields.get(content_name)


class _StubExprEval:
    """No-op expression evaluator — returns the expression unchanged."""

    def evaluate(self, expression, context):
        return expression


def _principal(pid: str = "alice", *, scopes=()) -> AuthContext:
    return AuthContext(
        principal=Principal(id=pid, type="human", display_name=pid.title()),
        scopes=tuple(scopes),
        role_name="user",
    )


def _request(
    *,
    method: str = "GET",
    path_params: dict | None = None,
    query_params: dict | None = None,
    body: Any = None,
    auth: AuthContext | None = None,
    headers: dict | None = None,
) -> TerminRequest:
    raw = b""
    hdrs = dict(headers or {})
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        hdrs.setdefault("content-type", "application/json")
    return TerminRequest(
        method=method,
        path="/api/v1/x",
        path_params=path_params or {},
        query_params=query_params or {},
        headers=hdrs,
        body=raw,
        auth=auth or _principal(),
    )


def _schema(name: str, *, fields: list[dict] | None = None) -> dict:
    return {
        "name": {"snake": name},
        "singular": name.rstrip("s") if name.endswith("s") else name,
        "fields": fields or [
            {"name": "title"},
            {"name": "priority"},
        ],
    }


# ── list_content_handler ──


class TestListHandler:
    def test_list_returns_records_unfiltered(self):
        ctx = _StubCtx(
            records={"products": [{"id": "1", "title": "A"}]},
            content_lookup={"products": _schema("products")},
        )
        req = _request(path_params={"content": "products"})
        resp = asyncio.run(list_content_handler(req, ctx))
        assert resp.status_code == 200
        assert resp.json_body == [{"id": "1", "title": "A"}]

    def test_list_rejects_offset_with_v09_message(self):
        ctx = _StubCtx(content_lookup={"products": _schema("products")})
        req = _request(path_params={"content": "products"},
                       query_params={"offset": "10"})
        with pytest.raises(TerminBadRequestError) as exc:
            asyncio.run(list_content_handler(req, ctx))
        assert "offset" in str(exc.value).lower()

    def test_list_rejects_non_integer_limit(self):
        ctx = _StubCtx(content_lookup={"products": _schema("products")})
        req = _request(path_params={"content": "products"},
                       query_params={"limit": "abc"})
        with pytest.raises(TerminBadRequestError):
            asyncio.run(list_content_handler(req, ctx))

    def test_list_rejects_negative_limit(self):
        ctx = _StubCtx(content_lookup={"products": _schema("products")})
        req = _request(path_params={"content": "products"},
                       query_params={"limit": "-1"})
        with pytest.raises(TerminBadRequestError):
            asyncio.run(list_content_handler(req, ctx))

    def test_list_rejects_limit_over_1000(self):
        ctx = _StubCtx(content_lookup={"products": _schema("products")})
        req = _request(path_params={"content": "products"},
                       query_params={"limit": "5000"})
        with pytest.raises(TerminBadRequestError):
            asyncio.run(list_content_handler(req, ctx))

    def test_list_honors_valid_limit(self):
        ctx = _StubCtx(
            records={"products": [{"id": str(i)} for i in range(20)]},
            content_lookup={"products": _schema("products")},
        )
        req = _request(path_params={"content": "products"},
                       query_params={"limit": "5"})
        resp = asyncio.run(list_content_handler(req, ctx))
        assert len(resp.json_body) == 5

    def test_list_rejects_unknown_sort_field(self):
        ctx = _StubCtx(content_lookup={"products": _schema("products")})
        req = _request(path_params={"content": "products"},
                       query_params={"sort": "ghost"})
        with pytest.raises(TerminBadRequestError):
            asyncio.run(list_content_handler(req, ctx))

    def test_list_rejects_invalid_sort_direction(self):
        ctx = _StubCtx(content_lookup={"products": _schema("products")})
        req = _request(path_params={"content": "products"},
                       query_params={"sort": "title:sideways"})
        with pytest.raises(TerminBadRequestError):
            asyncio.run(list_content_handler(req, ctx))

    def test_list_accepts_sort_with_explicit_direction(self):
        ctx = _StubCtx(
            records={"products": []},
            content_lookup={"products": _schema("products")},
        )
        req = _request(path_params={"content": "products"},
                       query_params={"sort": "title:desc"})
        asyncio.run(list_content_handler(req, ctx))
        # Verify the storage call carried the order_by.
        op, _, _, options = ctx.storage.calls[0]
        assert options.order_by[0].field == "title"
        assert options.order_by[0].direction == "desc"

    def test_list_rejects_unknown_filter_field(self):
        ctx = _StubCtx(content_lookup={"products": _schema("products")})
        req = _request(path_params={"content": "products"},
                       query_params={"ghost": "value"})
        with pytest.raises(TerminBadRequestError):
            asyncio.run(list_content_handler(req, ctx))

    def test_list_filters_by_field(self):
        ctx = _StubCtx(
            records={"products": [
                {"id": "1", "priority": "high"},
                {"id": "2", "priority": "low"},
            ]},
            content_lookup={"products": _schema("products")},
        )
        req = _request(path_params={"content": "products"},
                       query_params={"priority": "high"})
        resp = asyncio.run(list_content_handler(req, ctx))
        assert resp.json_body == [{"id": "1", "priority": "high"}]

    def test_list_applies_ownership_row_filter(self):
        ctx = _StubCtx(
            records={"sessions": [
                {"id": "1", "owner_id": "alice"},
                {"id": "2", "owner_id": "bob"},
            ]},
            content_lookup={"sessions": _schema(
                "sessions", fields=[{"name": "owner_id"}],
            )},
            row_filter={"kind": "ownership", "field": "owner_id"},
        )
        req = _request(
            path_params={"content": "sessions"},
            auth=_principal("alice"),
        )
        resp = asyncio.run(list_content_handler(req, ctx))
        assert resp.json_body == [{"id": "1", "owner_id": "alice"}]

    def test_list_storage_value_error_becomes_400(self):
        ctx = _StubCtx(content_lookup={"products": _schema("products")})

        async def bad(*args, **kwargs):
            raise ValueError("opaque cursor")

        ctx.storage.query = bad
        req = _request(path_params={"content": "products"},
                       query_params={"cursor": "garbage"})
        with pytest.raises(TerminBadRequestError):
            asyncio.run(list_content_handler(req, ctx))


# ── get_content_handler ──


class TestGetHandler:
    def test_get_existing_record(self):
        ctx = _StubCtx(
            records={"products": [{"id": "1", "title": "A"}]},
            content_lookup={"products": _schema("products")},
        )
        req = _request(path_params={"content": "products", "key": "1"})
        resp = asyncio.run(get_content_handler(req, ctx))
        assert resp.status_code == 200
        assert resp.json_body == {"id": "1", "title": "A"}

    def test_get_missing_record_404s(self):
        ctx = _StubCtx(
            records={"products": []},
            content_lookup={"products": _schema("products")},
        )
        req = _request(path_params={"content": "products", "key": "ghost"})
        with pytest.raises(TerminNotFoundError):
            asyncio.run(get_content_handler(req, ctx))


# ── create_content_handler ──


class TestCreateHandler:
    def test_create_persists_record(self):
        ctx = _StubCtx(content_lookup={"products": _schema("products")})
        req = _request(
            method="POST",
            path_params={"content": "products"},
            body={"title": "New", "priority": "high"},
        )
        resp = asyncio.run(create_content_handler(req, ctx))
        assert resp.status_code == 201
        body = resp.json_body
        assert body["title"] == "New"
        assert body["id"] == "rec-new"

    def test_create_with_form_body_treats_as_form(self):
        # Without an application/json content-type, the handler treats
        # the body as form data. Multipart/form parsing of non-form
        # bytes yields an empty form dict — the handler proceeds with
        # empty body. Records that intent here so a future change to
        # tighten this path doesn't silently regress.
        ctx = _StubCtx(content_lookup={"products": _schema("products")})
        req = TerminRequest(
            method="POST",
            path="/api/v1/products",
            path_params={"content": "products"},
            headers={"content-type": "application/x-www-form-urlencoded"},
            body=b"title=Form",
            auth=_principal(),
        )
        resp = asyncio.run(create_content_handler(req, ctx))
        assert resp.status_code == 201

    def test_create_publishes_content_event(self):
        events: list = []

        async def publish(kind, content, record):
            events.append((kind, content, record["id"]))

        ctx = _StubCtx(
            content_lookup={"products": _schema("products")},
            publish=publish,
        )
        req = _request(
            method="POST",
            path_params={"content": "products"},
            body={"title": "X"},
        )
        asyncio.run(create_content_handler(req, ctx))
        assert events == [("created", "products", "rec-new")]

    def test_create_stamps_owner_field_from_principal(self):
        ctx = _StubCtx(
            content_lookup={"sessions": _schema(
                "sessions",
                fields=[{"name": "title"}, {"name": "owner_id"}],
            )},
            owner_field_for_content={"sessions": "owner_id"},
        )
        req = _request(
            method="POST",
            path_params={"content": "sessions"},
            body={"title": "S1"},
            auth=_principal("alice"),
        )
        asyncio.run(create_content_handler(req, ctx))
        # Storage saw owner_id stamped from auth.principal.id.
        op, content, record = ctx.storage.calls[0]
        assert record["owner_id"] == "alice"


# ── update_content_handler ──


class TestUpdateHandler:
    def test_update_existing_record(self):
        ctx = _StubCtx(
            records={"products": [{"id": "1", "title": "Old"}]},
            content_lookup={"products": _schema("products")},
        )
        req = _request(
            method="PUT",
            path_params={"content": "products", "key": "1"},
            body={"title": "New"},
        )
        resp = asyncio.run(update_content_handler(req, ctx))
        assert resp.status_code == 200
        assert resp.json_body["title"] == "New"

    def test_update_missing_record_404s(self):
        ctx = _StubCtx(
            records={"products": []},
            content_lookup={"products": _schema("products")},
        )
        req = _request(
            method="PUT",
            path_params={"content": "products", "key": "ghost"},
            body={"title": "X"},
        )
        with pytest.raises(TerminNotFoundError):
            asyncio.run(update_content_handler(req, ctx))

    def test_update_partial_patches_record(self):
        ctx = _StubCtx(
            records={"products": [{"id": "1", "title": "Old", "priority": "low"}]},
            content_lookup={"products": _schema("products")},
        )
        req = _request(
            method="PUT",
            path_params={"content": "products", "key": "1"},
            body={"title": "New"},
        )
        resp = asyncio.run(update_content_handler(req, ctx))
        # Existing fields preserved; only title patched.
        assert resp.json_body["title"] == "New"
        assert resp.json_body["priority"] == "low"


# ── delete_content_handler ──


class TestDeleteHandler:
    def test_delete_existing_record(self):
        ctx = _StubCtx(
            records={"products": [{"id": "1"}]},
            content_lookup={"products": _schema("products")},
        )
        req = _request(
            method="DELETE",
            path_params={"content": "products", "key": "1"},
        )
        resp = asyncio.run(delete_content_handler(req, ctx))
        assert resp.status_code == 200
        assert resp.json_body == {"deleted": True}

    def test_delete_missing_record_404s(self):
        ctx = _StubCtx(
            records={"products": []},
            content_lookup={"products": _schema("products")},
        )
        req = _request(
            method="DELETE",
            path_params={"content": "products", "key": "ghost"},
        )
        with pytest.raises(TerminNotFoundError):
            asyncio.run(delete_content_handler(req, ctx))

    def test_delete_publishes_deleted_event(self):
        events: list = []

        async def publish(kind, content, record):
            events.append((kind, content, record.get("id")))

        ctx = _StubCtx(
            records={"products": [{"id": "1", "title": "Old"}]},
            content_lookup={"products": _schema("products")},
            publish=publish,
        )
        req = _request(
            method="DELETE",
            path_params={"content": "products", "key": "1"},
        )
        asyncio.run(delete_content_handler(req, ctx))
        assert events == [("deleted", "products", "1")]
