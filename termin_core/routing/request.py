# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Request / response value types — the framework-agnostic surface
that route handlers operate on.

Built on top of ASGI semantics (per Q1 of the Phase 7 design doc):
adapters parse ASGI scope/receive once at the boundary and hand
handlers a :class:`TerminRequest`. Handlers return a
:class:`TerminResponse`; adapters translate back to ASGI send
events.

The substrate is ASGI but neither this module nor the rest of
termin-core actually imports any ASGI library — these types are
pure dataclasses. ASGI-shaped adapters (FastAPI, Starlette, Quart,
plain uvicorn) construct them; non-ASGI adapters (a CLI runtime,
a JSON-RPC server) can construct them too if useful.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, TYPE_CHECKING
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from ..providers.identity_contract import Principal
    from .auth import AuthContext


class _CaseInsensitiveDict(dict):
    """Header / cookie dict with case-insensitive key lookup.

    HTTP header names are case-insensitive per RFC 7230 §3.2; users
    of this class can call ``headers["content-type"]`` regardless of
    whether the adapter stored it as ``Content-Type`` or ``CONTENT-TYPE``.
    Stored keys preserve their original casing for adapters that
    care (e.g., for response generation).

    Cookies are conventionally case-sensitive but Termin accepts
    either; using the same dict shape keeps adapter code uniform.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._lc_index: dict[str, str] = {}
        if args or kwargs:
            self.update(*args, **kwargs)

    def __setitem__(self, key: str, value: Any) -> None:
        lc = str(key).lower()
        # Drop any existing entry with the same case-folded key so we
        # don't accumulate duplicates on case-mismatching writes.
        if lc in self._lc_index:
            super().__delitem__(self._lc_index[lc])
        self._lc_index[lc] = key
        super().__setitem__(key, value)

    def __getitem__(self, key: str) -> Any:
        lc = str(key).lower()
        if lc in self._lc_index:
            return super().__getitem__(self._lc_index[lc])
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._lc_index

    def __delitem__(self, key: str) -> None:
        lc = str(key).lower()
        if lc not in self._lc_index:
            raise KeyError(key)
        original = self._lc_index.pop(lc)
        super().__delitem__(original)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def update(self, *args: Any, **kwargs: Any) -> None:
        # Defer to __setitem__ so the lowercase index stays consistent.
        if args:
            other = args[0]
            if hasattr(other, "items"):
                for k, v in other.items():
                    self[k] = v
            else:
                for k, v in other:
                    self[k] = v
        for k, v in kwargs.items():
            self[k] = v


@dataclass
class TerminRequest:
    """A request the runtime can dispatch.

    Adapters construct this from their framework's request type and
    hand it to a route handler. Handlers operate on it directly —
    no framework-specific decorators, no dependency injection
    machinery, just the data.

    Attributes:
        method: HTTP method (``GET``, ``POST``, ``PUT``, ``DELETE``,
            ``PATCH``, etc.). Always uppercase.
        path: The URL path that matched, without query string.
            E.g. ``/api/v1/products/42``.
        path_params: Path parameters extracted by the adapter's
            router. E.g. ``{"id": "42"}`` for ``/api/v1/products/{id}``.
        query_params: Parsed query string. Adapters that receive
            multiple values for the same key (e.g., ``?tag=a&tag=b``)
            should provide the last value via this dict; callers
            that need all values use :attr:`query_params_multi`.
        query_params_multi: All values for each query key, in the
            order they appeared. Convenient for filter UIs that
            allow repeated parameters.
        headers: Request headers. Case-insensitive lookup —
            ``headers["content-type"]`` works regardless of the wire
            casing.
        cookies: Cookie name → value. Case-sensitive.
        body: Raw request body bytes. Empty for GET / DELETE
            requests in normal use; populated for POST / PUT / PATCH.
        principal: The authenticated caller, populated by the
            adapter's principal-extraction middleware before the
            handler runs (per Q3=a of the routing briefing). May be
            None when no identity provider is bound; handlers should
            tolerate that as the anonymous case.
        auth: Resolved authentication context — principal plus
            request-scoped scopes plus role name. Adapter
            middleware computes once and assigns. Handlers consume
            ``request.auth.has_scope(...)`` for authorization
            questions. May be None when no identity provider is
            bound; handlers should treat that as the anonymous,
            no-scopes case.
        scheme: ``http`` or ``https``.
        client: ``(host, port)`` tuple of the immediate client, when
            the adapter can determine it.
    """

    method: str
    path: str
    path_params: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)
    query_params_multi: dict[str, list[str]] = field(default_factory=dict)
    headers: _CaseInsensitiveDict = field(default_factory=_CaseInsensitiveDict)
    cookies: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    principal: Optional["Principal"] = None
    auth: Optional["AuthContext"] = None
    scheme: str = "http"
    client: Optional[tuple[str, int]] = None
    # Slice 7.2.e transitional: the legacy
    # ``ctx.get_current_user(request)`` dict the v0.9 runtime
    # threaded through CEL evaluation. It carries the
    # ``User.Username`` / ``User.Role`` etc. PascalCase shape that
    # IR-declared default_expr / dependent_values expressions
    # reference. Adapter middleware sets this so handlers calling
    # ``evaluate_field_defaults`` keep working during the
    # migration. Slice 7.5 deletes this field once the legacy
    # CEL-resolved shape moves into AuthContext or is replaced.
    legacy_user_dict: Optional[dict] = None

    def __post_init__(self) -> None:
        # Normalize method to uppercase (adapters sometimes pass
        # lowercase from raw ASGI scopes).
        self.method = self.method.upper()
        # Promote a plain dict to case-insensitive if the caller
        # passed one — saves boilerplate at every adapter boundary.
        if not isinstance(self.headers, _CaseInsensitiveDict):
            self.headers = _CaseInsensitiveDict(self.headers)

    async def json(self) -> Any:
        """Parse the request body as JSON. Returns the decoded
        value (typically a dict or list). Raises ValueError if the
        body isn't valid JSON.
        """
        if not self.body:
            return None
        return _json.loads(self.body.decode("utf-8"))

    async def form(self) -> dict[str, str]:
        """Parse the request body as URL-encoded form data. Returns
        a dict of name → first-value. Use :meth:`form_multi` for
        repeat fields.
        """
        if not self.body:
            return {}
        text = self.body.decode("utf-8")
        # parse_qs returns list[str] per key; collapse to first value
        # for the common case.
        parsed = parse_qs(text, keep_blank_values=True)
        return {k: v[0] if v else "" for k, v in parsed.items()}

    async def form_multi(self) -> dict[str, list[str]]:
        """Parse the request body as URL-encoded form data with
        repeat-field preservation."""
        if not self.body:
            return {}
        text = self.body.decode("utf-8")
        return parse_qs(text, keep_blank_values=True)


@dataclass
class TerminResponse:
    """A response the runtime hands back to the adapter.

    Adapters translate this back to their framework's response type
    (FastAPI :class:`Response`, Starlette :class:`Response`, plain
    ASGI send events, etc.). One handler returns one
    :class:`TerminResponse`; the adapter is responsible for emitting
    the bytes on the wire.

    Two body fields, mutually exclusive in the common case:
        - :attr:`json_body` — a Python value to be JSON-serialized.
          Adapter sets ``Content-Type: application/json`` if no
          override in :attr:`headers`.
        - :attr:`body` — raw bytes. Adapter ships as-is.
        - :attr:`streaming` — async iterator of chunks for SSE /
          large responses. Adapters enable streaming send mode.

    For redirects, set :attr:`redirect_url`; the adapter emits the
    matching ``Location`` header and ignores the body fields.

    Attributes:
        status_code: HTTP status code (default 200). Set to 201 for
            create routes, 204 for empty responses, etc.
        headers: Response headers. Case-insensitive write; the
            adapter sets the canonical casing on the wire.
        body: Raw response body bytes. Adapter doesn't touch
            Content-Type when this is set.
        json_body: Python value to be JSON-serialized into the body.
            Adapter sets Content-Type: application/json unless an
            override is in :attr:`headers`.
        redirect_url: If set, the adapter emits a 303 (or whatever
            :attr:`status_code` says, default 303 if unchanged from
            200) with ``Location: <redirect_url>`` and an empty
            body. Body fields are ignored.
        streaming: Async iterator of byte chunks. Adapter switches
            into streaming mode and writes each chunk as it
            arrives. Mutually exclusive with body / json_body.
        media_type: Optional explicit Content-Type override. Wins
            over the json_body / body defaults.
    """

    status_code: int = 200
    headers: _CaseInsensitiveDict = field(default_factory=_CaseInsensitiveDict)
    body: Optional[bytes] = None
    json_body: Any = None
    redirect_url: Optional[str] = None
    streaming: Optional[AsyncIterator[bytes]] = None
    media_type: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.headers, _CaseInsensitiveDict):
            self.headers = _CaseInsensitiveDict(self.headers)
        # Default 303 for redirects when caller didn't override.
        if self.redirect_url and self.status_code == 200:
            self.status_code = 303


__all__ = ["TerminRequest", "TerminResponse"]
