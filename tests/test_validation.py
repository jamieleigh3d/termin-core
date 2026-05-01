# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Direct unit tests for ``termin_core.validation.dependents``.

Exercises every constraint shape (one_of, equals, default), the
field-level one_of_values shortcut, the When predicate evaluation,
enum-value enforcement, min/max numeric bounds, evaluate_field_defaults
with the AuthContext-driven binding, and strip_unknown_fields. Coverage
target: bring validation/dependents.py from 38% (slice 7.5b post-CRUD)
to 90%+.
"""

from __future__ import annotations

import pytest

from termin_core.errors import TerminValidationError
from termin_core.providers.identity_contract import Principal
from termin_core.routing import AuthContext
from termin_core.validation import (
    evaluate_field_defaults,
    strip_unknown_fields,
    validate_dependent_values,
    validate_enum_constraints,
    validate_min_max_constraints,
)


class _StubExprEval:
    """Trivial CEL evaluator: looks up the expression as a key in the
    context dict. Tests pass expressions like 'priority' to mean
    'context["priority"]', which is enough to exercise the
    when/conditional branches without dragging in cel-python."""

    def evaluate(self, expression, context):
        if isinstance(context, dict):
            return context.get(expression)
        return None


# ── one_of_values (field-level shortcut) ──


class TestFieldOneOfValues:
    def test_value_in_allowed_passes(self):
        schema = {"fields": [
            {"name": "priority", "one_of_values": ["low", "medium", "high"]},
        ]}
        validate_dependent_values("x", {"priority": "high"}, {"x": schema}, _StubExprEval())

    def test_value_not_in_allowed_raises(self):
        schema = {"fields": [
            {"name": "priority", "one_of_values": ["low", "medium", "high"]},
        ]}
        with pytest.raises(TerminValidationError):
            validate_dependent_values(
                "x", {"priority": "urgent"}, {"x": schema}, _StubExprEval(),
            )

    def test_empty_value_skips_check(self):
        # Empty string and None should NOT trigger validation —
        # they're handled by required-field validation elsewhere.
        schema = {"fields": [
            {"name": "priority", "one_of_values": ["low", "high"]},
        ]}
        validate_dependent_values("x", {"priority": ""}, {"x": schema}, _StubExprEval())
        validate_dependent_values("x", {"priority": None}, {"x": schema}, _StubExprEval())

    def test_numeric_one_of_coerces_string(self):
        schema = {"fields": [
            {"name": "level", "one_of_values": [1, 2, 3]},
        ]}
        # String "2" should coerce to int 2 and pass.
        validate_dependent_values("x", {"level": "2"}, {"x": schema}, _StubExprEval())

    def test_numeric_one_of_rejects_non_numeric(self):
        schema = {"fields": [
            {"name": "level", "one_of_values": [1, 2, 3]},
        ]}
        with pytest.raises(TerminValidationError):
            validate_dependent_values(
                "x", {"level": "abc"}, {"x": schema}, _StubExprEval(),
            )


# ── dependent_values constraints ──


class TestDependentOneOf:
    def test_unconditional_one_of(self):
        schema = {
            "fields": [{"name": "status"}],
            "dependent_values": [{
                "when": None,
                "field": "status",
                "constraint": "one_of",
                "values": ["open", "closed"],
            }],
        }
        validate_dependent_values("x", {"status": "open"}, {"x": schema}, _StubExprEval())
        with pytest.raises(TerminValidationError):
            validate_dependent_values(
                "x", {"status": "ghost"}, {"x": schema}, _StubExprEval(),
            )

    def test_when_condition_skips_when_false(self):
        # The when expression returns falsy → constraint is skipped.
        schema = {
            "fields": [{"name": "status"}],
            "dependent_values": [{
                "when": "is_premium",  # stub eval looks up data["is_premium"]
                "field": "status",
                "constraint": "one_of",
                "values": ["open"],
            }],
        }
        # "is_premium" missing from data → falsy → constraint skipped.
        validate_dependent_values(
            "x", {"status": "anything"}, {"x": schema}, _StubExprEval(),
        )

    def test_when_condition_fires_when_true(self):
        schema = {
            "fields": [{"name": "status"}],
            "dependent_values": [{
                "when": "is_premium",
                "field": "status",
                "constraint": "one_of",
                "values": ["open"],
            }],
        }
        # is_premium=True → constraint fires → 'closed' rejected.
        with pytest.raises(TerminValidationError):
            validate_dependent_values(
                "x", {"is_premium": True, "status": "closed"},
                {"x": schema}, _StubExprEval(),
            )


class TestDependentEquals:
    def test_equals_passes(self):
        schema = {
            "fields": [{"name": "color"}],
            "dependent_values": [{
                "when": None, "field": "color",
                "constraint": "equals", "value": "red",
            }],
        }
        validate_dependent_values(
            "x", {"color": "red"}, {"x": schema}, _StubExprEval(),
        )

    def test_equals_rejects_mismatch(self):
        schema = {
            "fields": [{"name": "color"}],
            "dependent_values": [{
                "when": None, "field": "color",
                "constraint": "equals", "value": "red",
            }],
        }
        with pytest.raises(TerminValidationError):
            validate_dependent_values(
                "x", {"color": "blue"}, {"x": schema}, _StubExprEval(),
            )


class TestDependentDefault:
    def test_default_fills_missing_field(self):
        schema = {
            "fields": [{"name": "priority"}],
            "dependent_values": [{
                "when": None, "field": "priority",
                "constraint": "default", "value": "medium",
            }],
        }
        data = {"title": "x"}
        validate_dependent_values("x", data, {"x": schema}, _StubExprEval())
        assert data["priority"] == "medium"

    def test_default_does_not_overwrite_present_field(self):
        schema = {
            "fields": [{"name": "priority"}],
            "dependent_values": [{
                "when": None, "field": "priority",
                "constraint": "default", "value": "medium",
            }],
        }
        data = {"priority": "high"}
        validate_dependent_values("x", data, {"x": schema}, _StubExprEval())
        assert data["priority"] == "high"


# ── enum constraints ──


class TestEnumConstraints:
    def test_enum_value_in_allowed_passes(self):
        schema = {"fields": [
            {"name": "priority", "enum_values": ["low", "high"]},
        ]}
        validate_enum_constraints({"priority": "high"}, schema)

    def test_enum_value_not_in_allowed_raises(self):
        schema = {"fields": [
            {"name": "priority", "enum_values": ["low", "high"]},
        ]}
        with pytest.raises(TerminValidationError):
            validate_enum_constraints({"priority": "urgent"}, schema)


# ── min/max numeric bounds ──


class TestMinMaxConstraints:
    def test_within_bounds_passes(self):
        schema = {"fields": [
            {"name": "score", "minimum": 0, "maximum": 100},
        ]}
        validate_min_max_constraints({"score": 50}, schema)

    def test_below_min_raises(self):
        schema = {"fields": [
            {"name": "score", "minimum": 0, "maximum": 100},
        ]}
        with pytest.raises(TerminValidationError):
            validate_min_max_constraints({"score": -1}, schema)

    def test_above_max_raises(self):
        schema = {"fields": [
            {"name": "score", "minimum": 0, "maximum": 100},
        ]}
        with pytest.raises(TerminValidationError):
            validate_min_max_constraints({"score": 200}, schema)

    def test_only_min_specified(self):
        schema = {"fields": [{"name": "score", "minimum": 0}]}
        validate_min_max_constraints({"score": 100}, schema)
        with pytest.raises(TerminValidationError):
            validate_min_max_constraints({"score": -1}, schema)

    def test_no_constraints_passes_anything(self):
        schema = {"fields": [{"name": "anything"}]}
        validate_min_max_constraints({"anything": 12345}, schema)


# ── evaluate_field_defaults ──


def _principal_auth(pid: str = "alice") -> AuthContext:
    return AuthContext(
        principal=Principal(id=pid, type="human", display_name=pid.title()),
        scopes=("read",),
        role_name="user",
    )


class TestEvaluateFieldDefaults:
    def test_evaluates_default_expr_using_the_user_binding(self):
        # default_expr 'the_user' (post-rewrite) returns the dict from
        # the context. The stub evaluator uses dict.get.
        schema = {"fields": [
            {"name": "owner", "default_expr": "the_user"},
        ]}
        data = {"title": "x"}
        evaluate_field_defaults(data, schema, _StubExprEval(), auth=_principal_auth())
        # the_user binding is a dict; default_expr eval returns the dict.
        assert isinstance(data.get("owner"), dict)
        assert data["owner"]["id"] == "alice"

    def test_skips_default_when_field_already_set(self):
        schema = {"fields": [
            {"name": "owner", "default_expr": "the_user"},
        ]}
        data = {"owner": "preset"}
        evaluate_field_defaults(data, schema, _StubExprEval(), auth=_principal_auth())
        assert data["owner"] == "preset"

    def test_evaluation_failure_silently_skips(self):
        # Stub returns None for unknown keys, but evaluator that
        # raises should not propagate — defaults are best-effort.
        class _BadEval:
            def evaluate(self, expression, context):
                raise RuntimeError("simulated CEL error")

        schema = {"fields": [
            {"name": "x", "default_expr": "anything"},
        ]}
        data = {}
        evaluate_field_defaults(data, schema, _BadEval(), auth=_principal_auth())
        assert "x" not in data  # no value set, no exception raised

    def test_anonymous_auth_yields_anonymous_binding(self):
        schema = {"fields": [
            {"name": "owner", "default_expr": "the_user"},
        ]}
        data = {}
        evaluate_field_defaults(data, schema, _StubExprEval(), auth=None)
        assert data.get("owner", {}).get("is_anonymous") is True


# ── strip_unknown_fields ──


class TestStripUnknownFields:
    def test_strips_unknown(self):
        schema = {"fields": [{"name": "title"}, {"name": "priority"}]}
        out = strip_unknown_fields({"title": "x", "ghost": "y"}, schema)
        assert out == {"title": "x"}

    def test_status_always_passes(self):
        schema = {"fields": [{"name": "title"}]}
        out = strip_unknown_fields({"title": "x", "status": "open"}, schema)
        assert out == {"title": "x", "status": "open"}

    def test_empty_data_yields_empty(self):
        schema = {"fields": [{"name": "title"}]}
        assert strip_unknown_fields({}, schema) == {}
