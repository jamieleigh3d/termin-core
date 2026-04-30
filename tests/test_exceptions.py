# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the framework-agnostic exception hierarchy.

Adapters need these exceptions to carry the information required to
translate a runtime failure into their framework's error envelope.
The tests below pin the contract: status_code is class-level (so
adapters can register one handler per class), detail and extra ride
on the instance.
"""

import pytest

from termin_core.errors import (
    TerminRuntimeError,
    TerminBadRequestError,
    TerminScopeError,
    TerminNotFoundError,
    TerminConflictError,
    TerminValidationError,
)


class TestStatusCodeMapping:
    """Each subclass declares the HTTP-status-equivalent the HTTP
    adapter should emit. Non-HTTP adapters can ignore or remap, but
    the class-level constant is the canonical mapping."""

    def test_bad_request_is_400(self):
        assert TerminBadRequestError.status_code == 400

    def test_scope_is_403(self):
        assert TerminScopeError.status_code == 403

    def test_not_found_is_404(self):
        assert TerminNotFoundError.status_code == 404

    def test_conflict_is_409(self):
        assert TerminConflictError.status_code == 409

    def test_validation_is_422(self):
        assert TerminValidationError.status_code == 422

    def test_base_default_is_500(self):
        """The base class defaults to 500 — adapters that catch the
        base class without a more-specific match treat it as an
        internal error."""
        assert TerminRuntimeError.status_code == 500


class TestSubclassHierarchy:
    """All concrete failures inherit from TerminRuntimeError so a
    single ``except TerminRuntimeError:`` catches them all in adapter
    code that wants generic handling."""

    @pytest.mark.parametrize("cls", [
        TerminBadRequestError,
        TerminScopeError,
        TerminNotFoundError,
        TerminConflictError,
        TerminValidationError,
    ])
    def test_subclass_of_termin_runtime_error(self, cls):
        assert issubclass(cls, TerminRuntimeError)

    @pytest.mark.parametrize("cls", [
        TerminBadRequestError,
        TerminScopeError,
        TerminNotFoundError,
        TerminConflictError,
        TerminValidationError,
        TerminRuntimeError,
    ])
    def test_subclass_of_exception(self, cls):
        """All Termin runtime errors are first-class Python
        exceptions — they raise / catch normally."""
        assert issubclass(cls, Exception)


class TestInstanceShape:
    """Detail message and structured extras ride on the instance."""

    def test_detail_only(self):
        err = TerminValidationError("must be one of: red, green, blue")
        assert err.detail == "must be one of: red, green, blue"
        assert err.extra == {}
        assert str(err) == "must be one of: red, green, blue"

    def test_detail_with_extra(self):
        err = TerminValidationError(
            "must be one of: red, green, blue",
            extra={"field": "color", "allowed": ["red", "green", "blue"]},
        )
        assert err.detail == "must be one of: red, green, blue"
        assert err.extra == {
            "field": "color",
            "allowed": ["red", "green", "blue"],
        }

    def test_extra_keyword_only(self):
        """`extra` must be a keyword-only argument so callers can't
        accidentally swap it with detail."""
        with pytest.raises(TypeError):
            TerminValidationError("msg", {"field": "color"})

    def test_repr_includes_status_code(self):
        err = TerminConflictError("undeclared transition: pending -> closed")
        r = repr(err)
        assert "TerminConflictError" in r
        assert "409" in r

    def test_extra_dict_is_copied(self):
        """Mutating the dict the caller passed mustn't change the
        instance's extra after construction."""
        original = {"field": "color"}
        err = TerminValidationError("bad value", extra=original)
        original["field"] = "size"
        assert err.extra == {"field": "color"}


class TestRaiseAndCatch:
    """End-to-end smoke: raise / except works the same as any
    Python exception."""

    def test_raise_then_except_specific(self):
        with pytest.raises(TerminScopeError) as excinfo:
            raise TerminScopeError("scope 'admin' required")
        assert excinfo.value.status_code == 403
        assert excinfo.value.detail == "scope 'admin' required"

    def test_raise_then_except_base(self):
        """An adapter can catch the base class as a generic handler."""
        with pytest.raises(TerminRuntimeError) as excinfo:
            raise TerminNotFoundError("no record with id=42")
        assert excinfo.value.status_code == 404
        assert isinstance(excinfo.value, TerminNotFoundError)

    def test_does_not_subclass_non_termin_exceptions(self):
        """The hierarchy must NOT subclass FastAPI's HTTPException
        or anything else framework-specific. Adapters bridge; the
        core stays framework-free."""
        # The framework-free guard in test_smoke.py covers the
        # import-graph dimension of this; here we check the
        # inheritance chain on the exception classes themselves.
        for cls in (
            TerminRuntimeError,
            TerminBadRequestError,
            TerminScopeError,
            TerminNotFoundError,
            TerminConflictError,
            TerminValidationError,
        ):
            for base in cls.__mro__:
                assert not base.__module__.startswith("fastapi"), (
                    f"{cls.__name__} inherits from {base.__module__}."
                    f"{base.__name__}; termin-core exceptions must "
                    f"stay framework-free.")
                assert not base.__module__.startswith("starlette"), (
                    f"{cls.__name__} inherits from {base.__module__}."
                    f"{base.__name__}; termin-core exceptions must "
                    f"stay framework-free.")
