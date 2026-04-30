# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Confidentiality / redaction surface.

Pure rules for how confidentiality scopes propagate from content
schemas to records, and how field values get redacted before
returning to a caller whose scope set doesn't satisfy the field's
required scopes. Used by both the storage layer (for read-time
redaction) and the compute layer (for taint integrity checks on
agent inputs/outputs).

No IO, no framework dependency — the rule set is pure data
transformation.
"""

from .redaction import (  # noqa: F401
    effective_scopes,
    redact_record,
    redact_records,
    is_redacted,
    check_write_access,
    check_compute_access,
    check_taint_integrity,
    enforce_output_taint,
    check_for_redacted_values,
)

__all__ = [
    "effective_scopes",
    "redact_record",
    "redact_records",
    "is_redacted",
    "check_write_access",
    "check_compute_access",
    "check_taint_integrity",
    "enforce_output_taint",
    "check_for_redacted_values",
]
