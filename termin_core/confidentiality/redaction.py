# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Confidentiality enforcement for the Termin runtime.

Provides field-level and content-level redaction of confidential data.
Records crossing API boundaries have restricted fields replaced with
redaction markers. The record itself is never omitted — even if every
field is redacted, the record remains present with its id and markers.

v0.9 Phase 5a.4 redaction marker format (per BRD #2 §7.6):
    {
        "__redacted": True,
        "field": <field_name>,
        "expected_type": <business_type>,
        "scope": <missing_scope>,        # legacy back-compat key
        "reason": <human-readable>       # provider-displayable
    }

Type-safety: presentation providers and consuming code can detect a
redacted value via `__redacted is True`. The `field` and
`expected_type` fields tell the provider which placeholder shape to
render (a redacted currency cell looks different from a redacted
boolean cell). The `scope` key is kept for back-compat with v0.8
consumers that key off it.

Pre-Phase-5a.4 shape: `{"__redacted": True, "scope": "<scope>"}`. The
new shape is a strict superset; old consumers checking only for
__redacted continue to work.
"""


def effective_scopes(field_ir: dict, content_ir: dict) -> set[str]:
    """Return the set of scopes required to see a field (AND semantics).

    Combines content-level scopes with field-level scopes. The caller
    must hold ALL scopes in the set to see the field unredacted.
    """
    scopes = set()
    scopes.update(content_ir.get("confidentiality_scopes") or [])
    scopes.update(field_ir.get("confidentiality_scopes") or [])
    return scopes


def _make_redaction_marker(field_name: str, field_ir: dict, missing_scope: str) -> dict:
    """Build the v0.9 Phase 5a.4 marker shape for one field.

    Single source of truth for marker construction — both the
    "field present in record" and "field absent from record" paths
    in :func:`redact_record` build markers via this helper so the
    shape stays consistent.
    """
    return {
        "__redacted": True,
        "field": field_name,
        "expected_type": field_ir.get("business_type", "text"),
        "scope": missing_scope,
        "reason": f"requires scope '{missing_scope}'",
    }


def redact_record(record: dict, content_ir: dict, caller_scopes: set[str]) -> dict:
    """Replace restricted field values with redaction markers.

    System fields (id, status for state machines) pass through unless
    they have explicit confidentiality scopes. Fields not in the
    schema also pass through (e.g., auto-generated id).

    **Absent-field marking (v0.9.4, closes termin-core #6 (5)).** For
    each declared field with a non-empty effective scope set, if the
    caller lacks the required scopes AND the field is absent from
    the record, the result carries a redacted marker for that field.
    This closes a storage-shape side channel: adapters whose
    underlying storage omits unset keys (DynamoDB, document stores)
    used to return records WITHOUT the confidential key, leaking the
    fact of absence to an unprivileged caller. After the fix every
    declared confidential field appears in the redacted output as a
    marker — the unprivileged caller cannot distinguish "no value"
    from "value present but hidden."

    Non-confidential fields absent from the record stay absent —
    only the confidentiality-scoped surface gets padded-as-marker
    behavior. Callers who hold the required scope see the field
    unchanged (and an absent non-confidential field stays absent
    for them too).

    v0.9 Phase 5a.4: marker shape carries `field` and `expected_type`
    per BRD #2 §7.6. The `scope` key is preserved for back-compat
    with v0.8 consumers; new consumers should use the expanded
    shape.
    """
    fields_by_name = {f["name"]: f for f in content_ir.get("fields", [])}
    result = {}
    # Pass 1: fields present in the record — redact in place or pass
    # through.
    for key, value in record.items():
        field_ir = fields_by_name.get(key)
        if field_ir is None:
            # System field (id) — always passes through
            result[key] = value
            continue
        required = effective_scopes(field_ir, content_ir)
        if required and not required.issubset(caller_scopes):
            missing = sorted(required - caller_scopes)
            result[key] = _make_redaction_marker(key, field_ir, missing[0])
        else:
            result[key] = value
    # Pass 2: confidentiality-scoped fields ABSENT from the record —
    # add a marker if the caller lacks the scope, so the storage's
    # representation of "absent" doesn't leak through as a side
    # channel. Skip when the caller has the scope (their privileged
    # view should reflect storage truth — absent stays absent).
    for field_ir in content_ir.get("fields", []):
        name = field_ir["name"]
        if name in record:
            continue
        required = effective_scopes(field_ir, content_ir)
        if not required:
            continue  # non-confidential — stay absent
        if required.issubset(caller_scopes):
            continue  # privileged caller — absence is truth
        missing = sorted(required - caller_scopes)
        result[name] = _make_redaction_marker(name, field_ir, missing[0])
    return result


def redact_records(records: list[dict], content_ir: dict, caller_scopes: set[str]) -> list[dict]:
    """Redact a list of records."""
    return [redact_record(r, content_ir, caller_scopes) for r in records]


def is_redacted(value) -> bool:
    """Check if a value is a redaction marker."""
    return isinstance(value, dict) and value.get("__redacted") is True


def check_write_access(fields_to_write: dict, content_ir: dict, caller_scopes: set[str]) -> str | None:
    """Check if the caller can write to all fields in the payload.

    Returns None if OK, or an error message if a restricted field is being written.
    """
    fields_by_name = {f["name"]: f for f in content_ir.get("fields", [])}
    for key in fields_to_write:
        field_ir = fields_by_name.get(key)
        if field_ir is None:
            continue  # unknown fields handled elsewhere
        required = effective_scopes(field_ir, content_ir)
        if required and not required.issubset(caller_scopes):
            missing = sorted(required - caller_scopes)
            return f"Cannot write to field '{key}' — requires scope '{missing[0]}'"
    return None


# ── Compute Confidentiality Checks (BRD Checks 1-4) ──

def check_compute_access(compute_ir: dict, caller_scopes: set[str]) -> str | None:
    """Check 1: Identity gate — reject if caller lacks required confidentiality scopes.

    In delegate mode, caller must have all required_confidentiality_scopes.
    In service mode, this check passes (service identity is auto-provisioned).

    Returns None if OK, or an error message.
    """
    if compute_ir.get("identity_mode") == "service":
        return None  # Service mode — scopes are auto-provisioned
    for scope in compute_ir.get("required_confidentiality_scopes", []):
        if scope not in caller_scopes:
            return f"Compute '{compute_ir['name']['display']}' requires confidentiality scope '{scope}'"
    return None


def check_taint_integrity(input_data: list[dict], content_ir: dict, delegate_scopes: set[str]) -> str | None:
    """Check 2: Taint integrity — detect unredacted confidential fields for unauthorized delegate.

    If a field should be redacted for the delegate but arrives unredacted,
    something upstream is broken. This is a defense-in-depth check.

    Returns None if OK, or an error message.
    """
    fields_by_name = {f["name"]: f for f in content_ir.get("fields", [])}
    for record in input_data:
        for fname, fval in record.items():
            field_ir = fields_by_name.get(fname)
            if field_ir is None:
                continue
            required = effective_scopes(field_ir, content_ir)
            if required and not required.issubset(delegate_scopes):
                if fval is not None and not is_redacted(fval):
                    return (f"Taint violation: field '{fname}' is unredacted "
                            f"for delegate lacking scope '{sorted(required - delegate_scopes)[0]}'")
    return None


def enforce_output_taint(output: dict, compute_ir: dict, delegate_scopes: set[str]) -> tuple[dict | None, str | None]:
    """Check 4: Output taint enforcement.

    Without reclassification, entire output is tainted by input scopes.
    With reclassification, output carries the declared scope.

    Returns (output, None) if OK, or (None, error_message) if blocked.
    """
    output_scope = compute_ir.get("output_confidentiality_scope")
    if output_scope:
        # Explicit reclassification — check delegate has the reclassified scope
        if output_scope not in delegate_scopes:
            return None, (f"Compute '{compute_ir['name']['display']}' reclassified output "
                         f"requires scope '{output_scope}'")
        return output, None

    # No reclassification — entire output tainted by input scopes
    for scope in compute_ir.get("required_confidentiality_scopes", []):
        if scope not in delegate_scopes:
            return None, (f"Compute '{compute_ir['name']['display']}' output tainted by "
                         f"scope '{scope}' — declare Output confidentiality to reclassify")
    return output, None


def check_for_redacted_values(value, path="") -> str | None:
    """Check 3: CEL redaction guard — detect redacted markers in values.

    Recursively checks if any value contains a __redacted marker.
    Returns None if clean, or an error message describing the redacted field.
    """
    if isinstance(value, dict):
        if value.get("__redacted"):
            scope = value.get("scope", "unknown")
            return f"Redacted field access at '{path}' (scope: {scope})"
        for k, v in value.items():
            err = check_for_redacted_values(v, f"{path}.{k}" if path else k)
            if err:
                return err
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            err = check_for_redacted_values(item, f"{path}[{i}]")
            if err:
                return err
    return None
