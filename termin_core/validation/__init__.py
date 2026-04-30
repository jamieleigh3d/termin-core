# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Validation rules — D-19 dependent values, enum constraints,
numeric bounds, mass-assignment stripping. Pure rules; no IO,
no framework dependency. Failures raise
:class:`termin_core.errors.TerminValidationError`; HTTP adapters
register a handler that translates to 422."""

from .dependents import (  # noqa: F401
    validate_dependent_values,
    validate_enum_constraints,
    validate_min_max_constraints,
    evaluate_field_defaults,
    strip_unknown_fields,
)

__all__ = [
    "validate_dependent_values",
    "validate_enum_constraints",
    "validate_min_max_constraints",
    "evaluate_field_defaults",
    "strip_unknown_fields",
]
