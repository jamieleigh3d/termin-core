# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Redaction must mark confidentiality-scoped fields the caller lacks
scope for, even when the field is absent from the record.

Background. Closes (5) of termin-core issue #6. An alt-runtime adopter
on a key-value storage (DynamoDB-style) reported that
``redact_record`` was returning records without the ``salary`` key
entirely when the caller lacked the read scope, instead of the
expected ``{"__redacted": True, ...}`` marker. The conformance test
(``test_confidentiality.TestFieldRedactionAPI``) does
``emp["salary"]["__redacted"]`` and got KeyError because the key
wasn't present.

Root cause: the previous ``redact_record`` only iterated
``record.items()`` and produced markers for fields PRESENT in the
record. The SQLite reference adapter pads every column on read
(NULL-padding), so the field is always there and gets correctly
replaced with a marker. Adapters whose underlying storage omits
absent fields (DynamoDB, sparse document stores) returned records
missing the field, and redaction silently passed the absence
through — exposing the absence/presence as a side channel.

Fix: after iterating ``record.items()``, also iterate the schema's
declared fields. For any confidentiality-scoped field NOT in the
record where the caller lacks the required scope, add a redacted
marker to the result. Non-confidential fields absent from the
record remain absent — only the confidentiality-scoped surface
gets the security guarantee.

This is a security-correctness fix: the absence of a confidential
field should never be a discoverable signal to a caller who can't
see the value.
"""

from termin_core.confidentiality.redaction import (
    redact_record,
    redact_records,
)


# ── Fixtures ──

_CONTENT_IR = {
    "name": "employees",
    "fields": [
        {"name": "id", "business_type": "id"},
        {"name": "display_name", "business_type": "text"},
        {
            "name": "salary",
            "business_type": "currency",
            "confidentiality_scopes": ["salary.access"],
        },
        {
            "name": "ssn",
            "business_type": "text",
            "confidentiality_scopes": ["pii.access"],
        },
        {
            "name": "notes",
            "business_type": "text",
            # Non-confidential — never redacted, absent if absent.
        },
    ],
}


# ── Tests for the existing happy path (records that include the field) ──


class TestRedactRecordPresentField:
    """Sanity: when the field is present in the record (with any
    value, including None), redaction replaces it with a marker."""

    def test_present_value_redacted(self):
        record = {"id": 1, "display_name": "Alice", "salary": 100000,
                  "ssn": "123-45-6789"}
        result = redact_record(record, _CONTENT_IR, caller_scopes=set())
        assert isinstance(result["salary"], dict)
        assert result["salary"]["__redacted"] is True
        assert result["salary"]["field"] == "salary"
        assert result["salary"]["expected_type"] == "currency"

    def test_present_none_redacted(self):
        """SQLite-style adapters return None for unset columns. The
        field is present, just with a None value — must still be
        redacted (and not leaked as 'no salary')."""
        record = {"id": 1, "display_name": "Alice", "salary": None}
        result = redact_record(record, _CONTENT_IR, caller_scopes=set())
        assert isinstance(result["salary"], dict)
        assert result["salary"]["__redacted"] is True

    def test_caller_with_scope_sees_value(self):
        record = {"id": 1, "display_name": "Alice", "salary": 100000}
        result = redact_record(
            record, _CONTENT_IR, caller_scopes={"salary.access"},
        )
        assert result["salary"] == 100000


# ── Tests for absent-field redaction (the issue #6 (5) fix) ──


class TestRedactRecordAbsentScopedField:
    """When the record lacks a confidentiality-scoped field entirely
    (DynamoDB-style storage, sparse stores) and the caller lacks the
    scope, the result must STILL carry a redacted marker for that
    field. Otherwise the absence is a side channel."""

    def test_absent_salary_gets_redacted_marker(self):
        record = {"id": 1, "display_name": "Alice"}  # no 'salary'
        result = redact_record(record, _CONTENT_IR, caller_scopes=set())
        assert "salary" in result, (
            "Confidentiality-scoped field absent from record must "
            "appear in the redacted output as a marker"
        )
        assert isinstance(result["salary"], dict)
        assert result["salary"]["__redacted"] is True
        assert result["salary"]["field"] == "salary"

    def test_absent_field_marker_carries_expected_type(self):
        record = {"id": 1, "display_name": "Alice"}
        result = redact_record(record, _CONTENT_IR, caller_scopes=set())
        assert result["salary"]["expected_type"] == "currency"
        assert result["ssn"]["expected_type"] == "text"

    def test_absent_field_marker_carries_scope(self):
        record = {"id": 1, "display_name": "Alice"}
        result = redact_record(record, _CONTENT_IR, caller_scopes=set())
        assert result["salary"]["scope"] == "salary.access"
        assert result["ssn"]["scope"] == "pii.access"

    def test_multiple_absent_scoped_fields_all_marked(self):
        record = {"id": 1, "display_name": "Alice"}
        result = redact_record(record, _CONTENT_IR, caller_scopes=set())
        # Both salary and ssn are confidentiality-scoped and absent
        # — both must appear as markers.
        assert isinstance(result["salary"], dict)
        assert isinstance(result["ssn"], dict)
        assert result["salary"]["__redacted"] is True
        assert result["ssn"]["__redacted"] is True

    def test_absent_field_skipped_when_caller_has_scope(self):
        """If the caller has the scope, an absent confidential field
        should NOT be padded with a marker — the absence carries no
        meaning to a privileged caller (whether the row has a value
        is the storage's truth, not a redaction concern)."""
        record = {"id": 1, "display_name": "Alice"}
        result = redact_record(
            record, _CONTENT_IR,
            caller_scopes={"salary.access", "pii.access"},
        )
        assert "salary" not in result
        assert "ssn" not in result

    def test_non_confidential_absent_field_stays_absent(self):
        """Non-confidential fields that are absent from the record
        stay absent — only confidentiality-scoped fields get the
        padding-as-marker treatment. ``notes`` is the test fixture's
        non-confidential field; it should never appear in the output
        if it isn't in the record."""
        record = {"id": 1, "display_name": "Alice"}
        result = redact_record(record, _CONTENT_IR, caller_scopes=set())
        assert "notes" not in result


class TestRedactRecordsList:
    """``redact_records`` is a list-shaped wrapper — same invariants
    must hold per record."""

    def test_list_apply_keeps_per_record_redaction(self):
        records = [
            {"id": 1, "display_name": "Alice"},                    # no salary
            {"id": 2, "display_name": "Bob", "salary": 200000},    # has salary
        ]
        results = redact_records(records, _CONTENT_IR, set())
        assert len(results) == 2
        # First record: salary absent, must be marked.
        assert isinstance(results[0]["salary"], dict)
        assert results[0]["salary"]["__redacted"] is True
        # Second record: salary present, must be marked.
        assert isinstance(results[1]["salary"], dict)
        assert results[1]["salary"]["__redacted"] is True


class TestContentLevelScopes:
    """Content-level confidentiality_scopes apply to all fields. An
    absent field inherits the content-level scope and gets marked
    when the caller lacks it."""

    _CONTENT_CLASSIFIED = {
        "name": "classified_docs",
        "confidentiality_scopes": ["clearance.secret"],
        "fields": [
            {"name": "id", "business_type": "id"},
            {"name": "title", "business_type": "text"},
        ],
    }

    def test_content_scope_marks_absent_field(self):
        """If the content carries a confidentiality scope, EVERY
        declared field — including absent ones — must be marked when
        the caller lacks the scope."""
        record = {"id": 1}  # no 'title'
        result = redact_record(
            record, self._CONTENT_CLASSIFIED, caller_scopes=set(),
        )
        assert "title" in result
        assert isinstance(result["title"], dict)
        assert result["title"]["__redacted"] is True
        assert result["title"]["scope"] == "clearance.secret"
