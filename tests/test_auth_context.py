# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for AuthContext — the routing-layer view of who's making
this request and what they can do here."""

import pytest

from termin_core.providers.identity_contract import (
    ANONYMOUS_PRINCIPAL,
    Principal,
)
from termin_core.routing import AuthContext, TerminRequest


@pytest.fixture
def alice():
    return Principal(
        id="okta:user-42",
        type="human",
        display_name="Alice",
    )


@pytest.fixture
def alice_auth(alice):
    return AuthContext(
        principal=alice,
        scopes=("orders.read", "orders.write", "products.read"),
        role_name="warehouse manager",
    )


class TestAuthContextConstruction:
    def test_minimal_construction(self, alice):
        ctx = AuthContext(principal=alice)
        assert ctx.principal is alice
        assert ctx.scopes == ()
        assert ctx.role_name == ""

    def test_full_construction(self, alice_auth, alice):
        assert alice_auth.principal is alice
        assert alice_auth.scopes == ("orders.read", "orders.write", "products.read")
        assert alice_auth.role_name == "warehouse manager"

    def test_frozen(self, alice_auth):
        """Immutable so it's safe to share across handler invocations."""
        with pytest.raises(Exception):  # FrozenInstanceError
            alice_auth.role_name = "different"


class TestHasScope:
    def test_has_scope_present(self, alice_auth):
        assert alice_auth.has_scope("orders.read")

    def test_has_scope_missing(self, alice_auth):
        assert not alice_auth.has_scope("admin.delete")

    def test_has_scope_empty(self):
        ctx = AuthContext(principal=ANONYMOUS_PRINCIPAL)
        assert not ctx.has_scope("anything")


class TestHasAny:
    def test_any_match_returns_true(self, alice_auth):
        assert alice_auth.has_any(("admin.delete", "orders.read"))

    def test_no_match_returns_false(self, alice_auth):
        assert not alice_auth.has_any(("admin.delete", "admin.create"))

    def test_empty_input_returns_false(self, alice_auth):
        assert not alice_auth.has_any([])

    def test_accepts_list(self, alice_auth):
        assert alice_auth.has_any(["orders.read"])

    def test_accepts_set(self, alice_auth):
        assert alice_auth.has_any({"orders.read"})


class TestHasAll:
    def test_all_present_returns_true(self, alice_auth):
        assert alice_auth.has_all(("orders.read", "orders.write"))

    def test_one_missing_returns_false(self, alice_auth):
        assert not alice_auth.has_all(("orders.read", "admin.delete"))

    def test_empty_input_returns_true(self, alice_auth):
        """Vacuously true — 'all of nothing' is true."""
        assert alice_auth.has_all([])


class TestIsAnonymous:
    def test_anonymous_principal_is_anonymous(self):
        ctx = AuthContext(principal=ANONYMOUS_PRINCIPAL)
        assert ctx.is_anonymous

    def test_named_principal_is_not_anonymous(self, alice_auth):
        assert not alice_auth.is_anonymous

    def test_empty_id_treated_as_anonymous(self):
        """Defensive — no identity provider should produce empty-id
        principals, but the check tolerates them."""
        empty = Principal(id="", type="human", display_name="")
        ctx = AuthContext(principal=empty)
        assert ctx.is_anonymous


class TestIsSystem:
    def test_system_principal_is_system(self):
        sys_p = Principal(
            id="system:scheduler",
            type="service",
            display_name="Scheduler",
            is_system=True,
        )
        ctx = AuthContext(principal=sys_p)
        assert ctx.is_system

    def test_human_is_not_system(self, alice_auth):
        assert not alice_auth.is_system

    def test_anonymous_is_not_system(self):
        ctx = AuthContext(principal=ANONYMOUS_PRINCIPAL)
        assert not ctx.is_system


class TestTerminRequestAuthField:
    """The auth field on TerminRequest is what handlers consume.
    Verify the wiring is correct end-to-end."""

    def test_auth_defaults_to_none(self):
        req = TerminRequest(method="GET", path="/")
        assert req.auth is None

    def test_auth_field_carries_context(self, alice_auth):
        req = TerminRequest(method="GET", path="/", auth=alice_auth)
        assert req.auth is alice_auth
        assert req.auth.has_scope("orders.read")

    def test_auth_and_principal_independently_settable(self, alice, alice_auth):
        """Some adapters may set principal without auth (e.g.,
        identity established but role-mapping not yet computed).
        Both fields are independent."""
        req_principal_only = TerminRequest(
            method="GET", path="/", principal=alice,
        )
        assert req_principal_only.principal is alice
        assert req_principal_only.auth is None

        req_both = TerminRequest(
            method="GET", path="/",
            principal=alice, auth=alice_auth,
        )
        assert req_both.principal is alice
        assert req_both.auth is alice_auth
