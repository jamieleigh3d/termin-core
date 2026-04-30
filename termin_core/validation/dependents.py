# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Request validation — dependent values, enum/min/max constraints.

D-19: Dependent value validation (When clauses, one_of, equals, default).
Field-level enum and numeric constraint enforcement.
Mass assignment protection (strip unknown fields).

Slice 7.2.b of Phase 7 (2026-04-30) moved this module from
``termin_runtime/validation.py`` into ``termin-core``. The functions
here raise :class:`termin_core.errors.TerminValidationError` instead
of ``fastapi.HTTPException``; HTTP adapters register a handler that
translates the exception into a 422 response. Non-HTTP adapters can
catch the exception however they like.
"""

from ..errors import TerminValidationError


def validate_dependent_values(content_name: str, data: dict,
                              content_lookup: dict, expr_eval) -> None:
    """Validate dependent value constraints (When clauses) on create/update.

    Evaluates all matching When conditions and validates constraints.
    Raises :class:`TerminValidationError` if a ``must be one of`` or
    ``must be`` constraint is violated. Adapter translates to 422.
    """
    schema = content_lookup.get(content_name, {})
    dep_vals = schema.get("dependent_values", [])

    # Validate field-level one_of_values constraints
    for field_def in schema.get("fields", []):
        fname = field_def["name"]
        one_of = field_def.get("one_of_values", [])
        if one_of and fname in data and data[fname] is not None and data[fname] != "":
            val = data[fname]
            if isinstance(one_of[0], (int, float)):
                try:
                    val = type(one_of[0])(val)
                except (ValueError, TypeError):
                    pass
            if val not in one_of:
                raise TerminValidationError(
                    f"Invalid value '{data[fname]}' for {fname}. "
                    f"Must be one of: {', '.join(str(v) for v in one_of)}",
                    extra={"field": fname, "allowed": list(one_of)},
                )

    if not dep_vals:
        return

    for dv in dep_vals:
        # Evaluate When condition (or unconditional if when is None)
        if dv.get("when"):
            try:
                matched = expr_eval.evaluate(dv["when"], data)
                if not matched:
                    continue
            except Exception:
                continue

        field_name = dv["field"]
        constraint = dv["constraint"]

        if constraint == "one_of":
            allowed = list(dv.get("values", []))
            if field_name in data and data[field_name] is not None and data[field_name] != "":
                val = data[field_name]
                if allowed and isinstance(allowed[0], (int, float)):
                    try:
                        val = type(allowed[0])(val)
                    except (ValueError, TypeError):
                        pass
                if val not in allowed:
                    when_desc = f" (when {dv['when']})" if dv.get("when") else ""
                    raise TerminValidationError(
                        f"Invalid value '{data[field_name]}' for "
                        f"{field_name}{when_desc}. "
                        f"Must be one of: {', '.join(str(v) for v in allowed)}",
                        extra={"field": field_name, "allowed": allowed,
                               "when": dv.get("when")},
                    )

        elif constraint == "equals":
            required_val = dv.get("value")
            if field_name in data and data[field_name] is not None:
                val = data[field_name]
                if isinstance(required_val, (int, float)):
                    try:
                        val = type(required_val)(val)
                    except (ValueError, TypeError):
                        pass
                if val != required_val:
                    when_desc = f" (when {dv['when']})" if dv.get("when") else ""
                    raise TerminValidationError(
                        f"Value for {field_name}{when_desc} must be "
                        f"{required_val}",
                        extra={"field": field_name, "required": required_val,
                               "when": dv.get("when")},
                    )

        elif constraint == "default":
            default_val = dv.get("value")
            if field_name not in data or data[field_name] is None or data[field_name] == "":
                data[field_name] = default_val


def validate_enum_constraints(data: dict, schema: dict) -> None:
    """Validate enum constraints on field values. Raises
    :class:`TerminValidationError` (adapter -> 422)."""
    for field_def in schema.get("fields", []):
        fname = field_def["name"]
        enum_vals = field_def.get("enum_values", [])
        if enum_vals and fname in data and data[fname]:
            if data[fname] not in enum_vals:
                raise TerminValidationError(
                    f"Invalid value '{data[fname]}' for {fname}. "
                    f"Must be one of: {', '.join(enum_vals)}",
                    extra={"field": fname, "allowed": list(enum_vals)},
                )


def validate_min_max_constraints(data: dict, schema: dict) -> None:
    """Validate min/max numeric constraints. Raises
    :class:`TerminValidationError` (adapter -> 422)."""
    for field_def in schema.get("fields", []):
        fname = field_def["name"]
        if fname not in data or data[fname] is None or data[fname] == "":
            continue
        try:
            val = float(data[fname])
        except (ValueError, TypeError):
            continue
        fmin = field_def.get("minimum")
        fmax = field_def.get("maximum")
        if fmin is not None and val < fmin:
            raise TerminValidationError(
                f"Value {val} for {fname} is below minimum {fmin}",
                extra={"field": fname, "value": val, "minimum": fmin},
            )
        if fmax is not None and val > fmax:
            raise TerminValidationError(
                f"Value {val} for {fname} exceeds maximum {fmax}",
                extra={"field": fname, "value": val, "maximum": fmax},
            )


def evaluate_field_defaults(data: dict, schema: dict, expr_eval, user: dict) -> None:
    """Evaluate ``default_expr`` for missing fields in-place. No
    validation failures emerge from this function — failed
    evaluation silently skips the default."""
    import datetime
    default_ctx = {
        "User": user.get("User", {}),
        "now": datetime.datetime.utcnow().isoformat() + "Z",
        "today": datetime.date.today().isoformat(),
    }
    for field_def in schema.get("fields", []):
        fname = field_def["name"]
        dexpr = field_def.get("default_expr")
        if dexpr and fname not in data:
            try:
                data[fname] = expr_eval.evaluate(dexpr, default_ctx)
            except Exception:
                pass


def strip_unknown_fields(data: dict, schema: dict) -> dict:
    """Strip unknown fields (mass-assignment protection)."""
    known_fields = {f["name"] for f in schema.get("fields", [])}
    known_fields.add("status")
    return {k: v for k, v in data.items() if k in known_fields}
