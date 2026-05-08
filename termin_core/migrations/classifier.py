# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Migration diff classifier.

Pure-function classifier per docs/migration-classifier-design.md
§3.3 (rules table) + §3.13 (rename folding) + §3.9
(empty-table downgrade — async pass).

The runtime calls these in order:

    diff = compute_migration_diff(current_schemas, target_schemas)
    diff = apply_rename_mappings(diff, deploy_rename_mappings)
    diff = await downgrade_for_empty_tables(diff, provider)

Then it block/ack-gates the diff and asks the provider to apply.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ..providers.storage_contract import (
    FieldChange, ContentChange, MigrationDiff, worst_classification,
)
from .errors import RenameMappingError


# ── Field-level classification ─────────────────────────────────────


# Lossless type widenings: from-type can be expressed in the to-type
# without information loss. (Per §3.3.)
_LOSSLESS_TYPE_WIDENINGS: frozenset = frozenset({
    ("whole_number", "number"),
    ("number", "text"),
    ("whole_number", "text"),
    ("currency", "text"),
    ("percentage", "text"),
    ("date", "text"),
    ("datetime", "text"),
    ("boolean", "text"),
    ("enum", "text"),
})


def _is_lossless_type_change(from_type: str, to_type: str) -> bool:
    if from_type == to_type:
        return True
    return (from_type, to_type) in _LOSSLESS_TYPE_WIDENINGS


def classify_field_change(
    change: FieldChange,
    *,
    field_spec: Optional[Mapping[str, Any]] = None,
) -> str:
    """Return the classification of one field-level change per §3.3.

    `field_spec` (when given) is the target FieldSpec the change is
    landing into — used to disambiguate "added" rules (optional vs
    required, default present, foreign_key present, etc.).

    Returns: "safe" | "low" | "medium" | "high" | "blocked".
    """
    kind = change.kind
    detail = change.detail or {}

    if kind == "added":
        spec = detail.get("spec") or field_spec or {}
        return _classify_field_added(spec)

    if kind == "removed":
        # Default classification is "blocked" (data loss);
        # downgrade_for_empty_tables() relaxes to "low" if the
        # table is empty.
        return "blocked"

    if kind == "renamed":
        # Field rename. Low if same type; medium if lossless type
        # change; high if lossy.
        type_changed = bool(detail.get("type_changed"))
        if not type_changed:
            return "low"
        from_t = detail.get("from_type", "")
        to_t = detail.get("to_type", "")
        if _is_lossless_type_change(from_t, to_t):
            return "medium"
        return "high"

    if kind == "type_changed":
        from_t = detail.get("from_type", "")
        to_t = detail.get("to_type", "")
        if not _is_lossless_type_change(from_t, to_t):
            return "blocked"
        # Lossless widening still requires SQLite rebuild → medium.
        return "medium"

    if kind == "required_added":
        # Adding NOT NULL: existing NULLs would violate.
        # Backfill required → high. Empty downgrade → low.
        return "high"
    if kind == "required_removed":
        return "medium"  # rebuild required, but loosening

    if kind == "unique_added":
        # Existing duplicates would violate → high. Empty → low.
        return "high"
    if kind == "unique_removed":
        return "medium"

    if kind == "bounds_changed":
        if detail.get("tightening"):
            return "high"
        return "medium"

    if kind == "enum_values_changed":
        # Adding values: medium (rebuild but safe). Removing values
        # whose data exists: high (operator must remap).
        if detail.get("removed"):
            return "high"
        return "medium"

    if kind == "cascade_mode_changed":
        # Per §3.3: cascade_mode change always requires the FK
        # rebuild dance, and future delete behavior changes for
        # existing records → high.
        return "high"

    if kind == "foreign_key_changed":
        from_fk = detail.get("from")
        to_fk = detail.get("to")
        if from_fk is None and to_fk is not None:
            # Adding FK to existing field — values may not exist
            # in target. Blocked unconditionally; runtime can't
            # validate referential integrity in advance.
            return "blocked"
        if from_fk is not None and to_fk is None:
            # Removing FK — loosening.
            return "medium"
        # Changing target — old values may not exist in new target.
        return "blocked"

    raise ValueError(f"unknown FieldChange.kind: {kind!r}")


def _classify_field_added(spec: Mapping[str, Any]) -> str:
    """Per §3.3 add-rules table.

    Required fields with default → safe (backfill is implicit in
    SQLite ADD COLUMN with default).
    Required fields without default → medium (operator should
    know existing rows take whatever default value is appropriate).
    Optional fields → safe.
    FK fields → blocked (can't satisfy cascade-or-restrict invariant
    without checking referential integrity).
    UNIQUE on add → medium (NULL doesn't violate UNIQUE in SQLite,
    but operator should know).
    """
    if spec.get("foreign_key"):
        return "blocked"
    is_required = bool(spec.get("required"))
    has_default = spec.get("default_expr") is not None
    is_unique = bool(spec.get("unique"))
    if is_unique:
        return "medium"
    if is_required and not has_default:
        return "medium"
    return "safe"


# ── Content-level classification ────────────────────────────────────


def classify_content_change(change: ContentChange) -> str:
    """Per §3.3.

    For "modified", aggregates field-change classifications worst-up.
    For other kinds, returns the kind-level rule directly.
    """
    if change.kind == "added":
        return "safe"
    if change.kind == "removed":
        # Default blocked; empty-downgrade pass relaxes to low.
        return "blocked"
    if change.kind == "renamed":
        return "low"
    if change.kind == "modified":
        if not change.field_changes:
            return "safe"  # vacuous modify
        target = change.schema or {}
        target_fields = {f["name"]: f for f in target.get("fields", [])}
        per_field = []
        for fc in change.field_changes:
            spec = target_fields.get(fc.field_name)
            per_field.append(classify_field_change(fc, field_spec=spec))
        return worst_classification(*per_field)
    raise ValueError(f"unknown ContentChange.kind: {change.kind!r}")


# ── Diff computation ────────────────────────────────────────────────


def compute_migration_diff(
    current: Optional[Sequence[Mapping[str, Any]]],
    target: Sequence[Mapping[str, Any]],
) -> MigrationDiff:
    """Compute a classified migration diff from current to target
    schemas.

    `current` is the previously-deployed IR's content schemas (a
    sequence of dicts), or None on a first-ever deploy.
    `target` is the to-be-deployed IR's content schemas.

    Returns a MigrationDiff with one ContentChange per affected
    content type, each carrying its own classification and (for
    modified) per-field FieldChanges.

    This is a pure function. Empty-table downgrade and rename
    folding happen in separate passes (downgrade_for_empty_tables,
    apply_rename_mappings).
    """
    if current is None:
        current = []

    by_name_current = {c["name"]["snake"]: c for c in current}
    by_name_target = {c["name"]["snake"]: c for c in target}

    changes = []

    # Removed contents: in current, not in target.
    for name in sorted(set(by_name_current) - set(by_name_target)):
        changes.append(ContentChange(
            kind="removed",
            content_name=name,
            classification="blocked",  # default; empty-downgrade may relax
        ))

    # Added contents: in target, not in current.
    for name in sorted(set(by_name_target) - set(by_name_current)):
        changes.append(ContentChange(
            kind="added",
            content_name=name,
            classification="safe",
            schema=by_name_target[name],
        ))

    # Modified contents: in both, but possibly different.
    for name in sorted(set(by_name_current) & set(by_name_target)):
        old_schema = by_name_current[name]
        new_schema = by_name_target[name]
        field_changes = _diff_fields(old_schema, new_schema)
        if not field_changes:
            continue  # no-op
        change = ContentChange(
            kind="modified",
            content_name=name,
            classification="safe",  # placeholder — overwritten below
            schema=new_schema,
            field_changes=tuple(field_changes),
        )
        # Recompute classification with full ContentChange in hand.
        cls = classify_content_change(change)
        # ContentChange is frozen; rebuild with the right classification.
        changes.append(ContentChange(
            kind="modified",
            content_name=name,
            classification=cls,
            schema=new_schema,
            field_changes=tuple(field_changes),
        ))

    return MigrationDiff(changes=tuple(changes))


def _diff_fields(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
) -> list:
    """Return a list of FieldChange entries describing how
    old_schema's fields differ from new_schema's. The `_state`
    business_type is special-cased — state-machine columns aren't
    listed in `fields` for storage purposes, so the differ only
    sees explicit IR fields."""
    old_fields = {f["name"]: f for f in old_schema.get("fields", [])}
    new_fields = {f["name"]: f for f in new_schema.get("fields", [])}
    changes: list = []

    for name in sorted(set(old_fields) - set(new_fields)):
        changes.append(FieldChange(kind="removed", field_name=name))
    for name in sorted(set(new_fields) - set(old_fields)):
        changes.append(FieldChange(
            kind="added",
            field_name=name,
            detail={"spec": dict(new_fields[name])},
        ))
    for name in sorted(set(old_fields) & set(new_fields)):
        old_f = old_fields[name]
        new_f = new_fields[name]
        changes.extend(_diff_one_field(name, old_f, new_f))
    return changes


def _diff_one_field(
    name: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
) -> list:
    """Emit FieldChange entries describing how `old` differs from
    `new` for a single field. Each constraint change is a separate
    FieldChange so the classifier can rule on it independently."""
    out: list = []
    if old.get("business_type") != new.get("business_type"):
        out.append(FieldChange(
            kind="type_changed",
            field_name=name,
            detail={
                "from_type": old.get("business_type", ""),
                "to_type": new.get("business_type", ""),
            },
        ))
    old_req = bool(old.get("required"))
    new_req = bool(new.get("required"))
    if old_req != new_req:
        out.append(FieldChange(
            kind="required_added" if new_req else "required_removed",
            field_name=name,
        ))
    old_uniq = bool(old.get("unique"))
    new_uniq = bool(new.get("unique"))
    if old_uniq != new_uniq:
        out.append(FieldChange(
            kind="unique_added" if new_uniq else "unique_removed",
            field_name=name,
        ))
    old_min, new_min = old.get("minimum"), new.get("minimum")
    old_max, new_max = old.get("maximum"), new.get("maximum")
    if (old_min, old_max) != (new_min, new_max):
        tightening = (
            (new_min is not None and (old_min is None or new_min > old_min))
            or (new_max is not None and (old_max is None or new_max < old_max))
        )
        out.append(FieldChange(
            kind="bounds_changed",
            field_name=name,
            detail={
                "from": {"min": old_min, "max": old_max},
                "to": {"min": new_min, "max": new_max},
                "tightening": tightening,
            },
        ))
    old_enum = tuple(old.get("enum_values") or ())
    new_enum = tuple(new.get("enum_values") or ())
    if old_enum != new_enum:
        added = tuple(v for v in new_enum if v not in old_enum)
        removed = tuple(v for v in old_enum if v not in new_enum)
        out.append(FieldChange(
            kind="enum_values_changed",
            field_name=name,
            detail={"added": added, "removed": removed},
        ))
    if old.get("cascade_mode") != new.get("cascade_mode"):
        # v0.8 → v0.9 case: old cascade_mode is None, new is "cascade"
        # or "restrict". Both directions are flagged the same way.
        out.append(FieldChange(
            kind="cascade_mode_changed",
            field_name=name,
            detail={
                "from": old.get("cascade_mode"),
                "to": new.get("cascade_mode"),
            },
        ))
    if old.get("foreign_key") != new.get("foreign_key"):
        out.append(FieldChange(
            kind="foreign_key_changed",
            field_name=name,
            detail={
                "from": old.get("foreign_key"),
                "to": new.get("foreign_key"),
            },
        ))
    return out


# ── Rename mapping (operator-declared, fold remove+add → renamed) ──


def apply_rename_mappings(
    diff: MigrationDiff,
    rename_fields: Sequence[Mapping[str, str]] = (),
    rename_contents: Sequence[Mapping[str, str]] = (),
) -> MigrationDiff:
    """Fold operator-declared renames per §3.13 into the diff.

    `rename_fields` is a sequence of `{content, from, to}` dicts
    declaring field renames within a content. `rename_contents`
    is a sequence of `{from, to}` dicts declaring content (table)
    renames.

    For each declared field rename: locate the matching
    `removed(from)` + `added(to)` FieldChange pair in the diff's
    "modified" ContentChange for `content`. Replace them with one
    `renamed` FieldChange.

    For each declared content rename: locate the matching
    `removed(from)` + `added(to)` ContentChange. Replace them with
    one `renamed` ContentChange.

    Validates the mapping (no cycles, no duplicate targets, no
    missing matches). Raises RenameMappingError on failure.
    """
    _validate_rename_mappings(rename_fields, rename_contents)

    new_changes: list = list(diff.changes)

    # Apply content renames first — they may also change the
    # content names that field renames target.
    for mapping in rename_contents:
        new_changes = _apply_one_content_rename(
            new_changes, mapping["from"], mapping["to"])

    for mapping in rename_fields:
        new_changes = _apply_one_field_rename(
            new_changes, mapping["content"], mapping["from"], mapping["to"])

    return MigrationDiff(changes=tuple(new_changes))


def _validate_rename_mappings(
    rename_fields: Sequence[Mapping[str, str]],
    rename_contents: Sequence[Mapping[str, str]],
) -> None:
    # Content rename: no cycles, no duplicate targets.
    seen_from = set()
    seen_to = set()
    for m in rename_contents:
        f, t = m.get("from"), m.get("to")
        if not f or not t or f == t:
            raise RenameMappingError(
                f"rename_contents entry must have distinct 'from' and 'to': "
                f"{m!r}",
                sub_code="TERMIN-M005",
            )
        if f in seen_from:
            raise RenameMappingError(
                f"duplicate rename_contents source: {f!r}",
                sub_code="TERMIN-M005",
            )
        if t in seen_to:
            raise RenameMappingError(
                f"duplicate rename_contents target: {t!r}",
                sub_code="TERMIN-M005",
            )
        seen_from.add(f)
        seen_to.add(t)
    # Cycle detection: target must not be the source of another rename.
    if seen_from & seen_to:
        raise RenameMappingError(
            f"rename_contents has a cycle: {sorted(seen_from & seen_to)!r}",
            sub_code="TERMIN-M005",
        )

    # Field rename: per content, no duplicate from/to.
    by_content: dict = {}
    for m in rename_fields:
        c = m.get("content")
        f, t = m.get("from"), m.get("to")
        if not c or not f or not t or f == t:
            raise RenameMappingError(
                f"rename_fields entry must have distinct 'from' and 'to' "
                f"and non-empty 'content': {m!r}",
                sub_code="TERMIN-M005",
            )
        seen_f, seen_t = by_content.setdefault(c, (set(), set()))
        if f in seen_f:
            raise RenameMappingError(
                f"duplicate rename_fields source in {c!r}: {f!r}",
                sub_code="TERMIN-M005",
            )
        if t in seen_t:
            raise RenameMappingError(
                f"duplicate rename_fields target in {c!r}: {t!r}",
                sub_code="TERMIN-M005",
            )
        seen_f.add(f)
        seen_t.add(t)


def _apply_one_content_rename(
    changes: list, from_name: str, to_name: str,
) -> list:
    """Replace a `removed(from_name)` + `added(to_name)` pair with a
    single `renamed` ContentChange."""
    removed_idx = next(
        (i for i, c in enumerate(changes)
         if c.kind == "removed" and c.content_name == from_name),
        None,
    )
    added_idx = next(
        (i for i, c in enumerate(changes)
         if c.kind == "added" and c.content_name == to_name),
        None,
    )
    if removed_idx is None or added_idx is None:
        raise RenameMappingError(
            f"rename_contents declares {from_name!r} → {to_name!r} but "
            f"the diff doesn't have a matching remove+add pair",
            sub_code="TERMIN-M006",
        )
    added_change = changes[added_idx]
    out = [c for i, c in enumerate(changes)
           if i != removed_idx and i != added_idx]
    out.append(ContentChange(
        kind="renamed",
        content_name=to_name,
        classification="low",  # ALTER TABLE RENAME TO is in-place
        schema=added_change.schema,
        detail={"from": from_name},
    ))
    return out


def _apply_one_field_rename(
    changes: list, content: str, from_name: str, to_name: str,
) -> list:
    """Locate the `modified(content)` ContentChange and fold its
    `removed(from)` + `added(to)` field changes into a single
    `renamed` FieldChange.

    If the from/to pair appears across a removed-content and an
    added-content (i.e., content rename hasn't been applied yet),
    raise — content rename must come first.
    """
    target_idx = next(
        (i for i, c in enumerate(changes)
         if c.kind == "modified" and c.content_name == content),
        None,
    )
    if target_idx is None:
        # Maybe the content was renamed and we should look for the new name?
        # The validator should've caught misordered cases; raise if not found.
        raise RenameMappingError(
            f"rename_fields declares {content}.{from_name} → "
            f"{content}.{to_name} but no modified ContentChange exists "
            f"for {content!r}",
            sub_code="TERMIN-M006",
        )
    cc = changes[target_idx]
    fc_list = list(cc.field_changes)
    removed_fc = next(
        (i for i, f in enumerate(fc_list)
         if f.kind == "removed" and f.field_name == from_name),
        None,
    )
    added_fc = next(
        (i for i, f in enumerate(fc_list)
         if f.kind == "added" and f.field_name == to_name),
        None,
    )
    if removed_fc is None or added_fc is None:
        raise RenameMappingError(
            f"rename_fields declares {content}.{from_name} → "
            f"{content}.{to_name} but the modified ContentChange for "
            f"{content!r} doesn't have a matching remove+add field pair",
            sub_code="TERMIN-M006",
        )
    new_spec = fc_list[added_fc].detail.get("spec") or {}
    target_fields = {f["name"]: f for f in (cc.schema or {}).get("fields", [])}
    new_field = target_fields.get(to_name) or new_spec
    # Look up old field type via the diff's surrounding context. The
    # FieldChange for "removed" doesn't carry the spec; we infer the
    # `type_changed` flag by comparing the new spec's business_type
    # against any "type_changed" entry on the same field.
    old_type = _infer_old_type(fc_list, from_name) or new_field.get("business_type", "")
    new_type = new_field.get("business_type", "")
    type_changed = old_type != new_type

    new_fcs = [f for i, f in enumerate(fc_list)
               if i != removed_fc and i != added_fc]
    new_fcs.append(FieldChange(
        kind="renamed",
        field_name=to_name,
        detail={
            "from": from_name,
            "to": to_name,
            "type_changed": type_changed,
            "from_type": old_type,
            "to_type": new_type,
        },
    ))
    new_cc = ContentChange(
        kind="modified",
        content_name=content,
        # placeholder; recompute below
        classification="safe",
        schema=cc.schema,
        field_changes=tuple(new_fcs),
    )
    new_cc = ContentChange(
        kind="modified",
        content_name=content,
        classification=classify_content_change(new_cc),
        schema=cc.schema,
        field_changes=tuple(new_fcs),
    )
    out = [c for c in changes]
    out[target_idx] = new_cc
    return out


def _infer_old_type(field_changes: list, field_name: str) -> Optional[str]:
    """If a `type_changed` FieldChange exists for the given field
    name, extract its from_type."""
    for fc in field_changes:
        if fc.kind == "type_changed" and fc.field_name == field_name:
            return fc.detail.get("from_type")
    return None


# ── Empty-table downgrade ──────────────────────────────────────────


# Field-change kinds whose "blocked" or "high" classification can
# downgrade to "low" when the affected table is empty (per §3.9).
_EMPTY_DOWNGRADE_FIELD_KINDS: frozenset = frozenset({
    "removed",
    "required_added",
    "unique_added",
    "bounds_changed",
    "enum_values_changed",
})


async def downgrade_for_empty_tables(
    diff: MigrationDiff, provider,
) -> MigrationDiff:
    """Async pass: for each modified/removed content with an empty
    table, downgrade the classification toward 'low' (NOT 'safe',
    per JL's review).

    `provider` is a StorageProvider with at least a query() method;
    the downgrade pass uses query-with-limit-1 to detect emptiness
    cheaply.
    """
    new_changes: list = []
    for change in diff.changes:
        if change.kind in ("added", "renamed"):
            new_changes.append(change)
            continue

        is_empty = await _is_table_empty(provider, change.content_name)

        if change.kind == "removed":
            if is_empty and change.classification == "blocked":
                new_changes.append(ContentChange(
                    kind="removed",
                    content_name=change.content_name,
                    classification="low",
                ))
                continue
            new_changes.append(change)
            continue

        if change.kind == "modified":
            if not is_empty:
                new_changes.append(change)
                continue
            # Empty: per-field downgrade.
            new_field_changes = []
            for fc in change.field_changes:
                if fc.kind in _EMPTY_DOWNGRADE_FIELD_KINDS:
                    # Treat as low risk on empty table.
                    new_field_changes.append(fc)  # field change unchanged
                else:
                    new_field_changes.append(fc)
            # Recompute by overriding classify to use "low" for
            # downgradable kinds when empty:
            cls = _classify_with_empty_downgrade(
                change.schema or {}, new_field_changes,
            )
            new_changes.append(ContentChange(
                kind="modified",
                content_name=change.content_name,
                classification=cls,
                schema=change.schema,
                field_changes=tuple(new_field_changes),
            ))
            continue

        new_changes.append(change)

    return MigrationDiff(changes=tuple(new_changes))


async def _is_table_empty(provider, content_name: str) -> bool:
    """Return True iff the named content type has zero rows.

    Uses the storage contract's query() method with limit=1, which
    every conforming provider must support. Falls back to assuming
    non-empty if anything goes wrong (safer default — keeps the
    diff at its original classification)."""
    try:
        from ..providers.storage_contract import QueryOptions
        page = await provider.query(
            content_name, None, QueryOptions(limit=1))
        return not page.records
    except Exception:
        # If query fails (e.g., the table doesn't exist yet — shouldn't
        # happen for "modified" or "removed" contents but defensive),
        # treat as non-empty so we don't downgrade away an actual
        # block.
        return False


def _classify_with_empty_downgrade(
    target_schema: Mapping[str, Any],
    field_changes: Sequence[FieldChange],
) -> str:
    """Variant of classify_content_change that treats empty-eligible
    field changes as low risk."""
    target_fields = {f["name"]: f for f in target_schema.get("fields", [])}
    per_field = []
    for fc in field_changes:
        if fc.kind in _EMPTY_DOWNGRADE_FIELD_KINDS:
            per_field.append("low")
        else:
            spec = target_fields.get(fc.field_name)
            per_field.append(classify_field_change(fc, field_spec=spec))
    if not per_field:
        return "safe"
    return worst_classification(*per_field)
