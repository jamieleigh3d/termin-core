# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Migration acknowledgment fingerprinting and ack-checking.

Per docs/migration-classifier-design.md §3.4 + §8.

Fingerprint format:
  FieldChange  : <content>.<field>:<change-kind>:<short-hash>
  ContentChange: <content>:<change-kind>:<short-hash>

Short-hash is 5 hex chars from SHA-256 of the change's structured
detail (kind-specific deterministic JSON encoding). Stable across
runs for the same change shape; changes when the change shape
changes, so an ack accepted yesterday no longer covers a different
change in today's IR.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence, Union

from ..providers.storage_contract import (
    FieldChange, ContentChange, MigrationDiff,
)


def fingerprint_change(
    change: Union[FieldChange, ContentChange],
    *,
    content_name: str | None = None,
) -> str:
    """Return a stable short fingerprint for the given change.

    For FieldChange, `content_name` is required (the change itself
    doesn't carry the content it belongs to; the caller threads it
    in from the surrounding ContentChange).
    """
    if isinstance(change, FieldChange):
        if content_name is None:
            raise ValueError(
                "fingerprint_change(FieldChange) requires content_name")
        body = _encode_field_change_body(change)
        digest = _short_hash(body)
        return f"{content_name}.{change.field_name}:{change.kind}:{digest}"
    if isinstance(change, ContentChange):
        body = _encode_content_change_body(change)
        digest = _short_hash(body)
        return f"{change.content_name}:{change.kind}:{digest}"
    raise TypeError(
        f"fingerprint_change expects FieldChange or ContentChange, "
        f"got {type(change).__name__}")


def _encode_field_change_body(change: FieldChange) -> str:
    """Deterministic JSON encoding of the load-bearing fields of a
    FieldChange. Excludes anything that might drift between
    semantically-equivalent diffs (e.g., dict ordering)."""
    body = {
        "kind": change.kind,
        "field_name": change.field_name,
        "detail": _normalize_detail(change.detail),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _encode_content_change_body(change: ContentChange) -> str:
    body = {
        "kind": change.kind,
        "content_name": change.content_name,
        "detail": _normalize_detail(change.detail),
    }
    # For "added" we deliberately omit the schema dict from the
    # fingerprint — otherwise tweaking a default value or display
    # name would invalidate the ack. The kind+name covers the
    # operationally-relevant identity.
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _normalize_detail(detail: Mapping[str, Any]) -> Any:
    """Normalize a detail dict for stable hashing.

    - Drop the "spec" subdict (FieldSpec for added fields) since its
      ordering and incidental keys would shift the fingerprint
      without changing semantics.
    - Convert tuples to lists.
    - Recursively sort dict keys.
    """
    if not detail:
        return {}
    out = {}
    for k, v in sorted(detail.items()):
        if k == "spec":
            # Only keep load-bearing flags from the spec.
            spec_v = v or {}
            out["spec"] = {
                "required": bool(spec_v.get("required")),
                "unique": bool(spec_v.get("unique")),
                "business_type": spec_v.get("business_type"),
                "foreign_key": spec_v.get("foreign_key"),
                "cascade_mode": spec_v.get("cascade_mode"),
            }
            continue
        if isinstance(v, dict):
            out[k] = _normalize_detail(v)
        elif isinstance(v, (list, tuple)):
            out[k] = list(v)
        else:
            out[k] = v
    return out


def _short_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:5]


# ── Acknowledgment checking ─────────────────────────────────────────


def collect_required_fingerprints(diff: MigrationDiff) -> tuple:
    """Return the tuple of fingerprints that need ack for this diff.

    A diff "needs ack" for any change classified low/medium/high
    (anything non-safe, non-blocked). Per BRD §6.2, blocked changes
    can never be acknowledged — they refuse the deploy. Per §3.4,
    safe changes apply silently.
    """
    out = []
    for cc in diff.changes:
        if cc.classification in ("safe", "blocked"):
            continue
        if cc.kind == "modified":
            # For modified contents, fingerprint the *field* changes
            # (each is acked individually) — gives operators
            # granular review.
            for fc in cc.field_changes:
                out.append(fingerprint_change(fc, content_name=cc.content_name))
        else:
            out.append(fingerprint_change(cc))
    return tuple(out)


def _blanket_low_active(migrations_config: Mapping[str, Any]) -> bool:
    """The dev-only blanket flag is in force iff BOTH dev_mode and
    accept_any_low are set. Either alone is inert. The combined gate
    is the production-strict default — operators must explicitly opt
    into developer conveniences."""
    if not migrations_config:
        return False
    return (
        bool(migrations_config.get("dev_mode"))
        and bool(migrations_config.get("accept_any_low"))
    )


def ack_covers(diff: MigrationDiff, migrations_config: Mapping[str, Any]) -> bool:
    """Return True iff the deploy config's `migrations` block covers
    every change that needs ack for this diff.

    A change is "covered" when:
      - Its fingerprint is in `accepted_changes` (per-change ack —
        always honored, any tier, any environment), OR
      - It is low-tier AND both `dev_mode: true` and
        `accept_any_low: true` are set (the blanket flag — dev-only,
        low-tier-only).

    Medium and high changes are never covered by the blanket flag,
    regardless of dev_mode. Blocked changes are never covered by
    anything (separately rejected before this is consulted).

    `migrations_config` shape:
        {
            "dev_mode": bool,                 # default False
            "accept_any_low": bool,           # default False
            "accepted_changes": [str, ...],   # default []
            "rename_fields": [...],           # consumed by classifier
            "rename_contents": [...],
        }
    """
    return not missing_acks(diff, migrations_config)


def missing_acks(
    diff: MigrationDiff, migrations_config: Mapping[str, Any],
) -> tuple:
    """Return the fingerprints required by the diff that are NOT
    covered by the deploy config. Empty tuple if everything's acked
    or covered by the dev-mode blanket-low flag."""
    if migrations_config is None:
        migrations_config = {}
    accepted = set(migrations_config.get("accepted_changes") or ())
    blanket_low = _blanket_low_active(migrations_config)

    missing: list[str] = []
    for cc in diff.changes:
        if cc.classification in ("safe", "blocked"):
            # Safe doesn't need ack; blocked is rejected separately.
            continue
        if cc.kind == "modified":
            for fc in cc.field_changes:
                fp = fingerprint_change(fc, content_name=cc.content_name)
                if fp in accepted:
                    continue
                tier = _classification_for_field_change(fc, cc)
                if tier == "low" and blanket_low:
                    continue
                if tier in ("safe", "blocked"):
                    # Field-level safe/blocked don't need ack at this
                    # level (blocked already failed; safe never did).
                    continue
                missing.append(fp)
        else:
            fp = fingerprint_change(cc)
            if fp in accepted:
                continue
            if cc.classification == "low" and blanket_low:
                continue
            missing.append(fp)
    return tuple(sorted(missing))


# ── Error formatters ────────────────────────────────────────────────


def format_blocked_error(diff: MigrationDiff) -> str:
    blocked = [c for c in diff.changes if c.classification == "blocked"]
    lines = [
        f"Termin migration refused — {len(blocked)} blocked change"
        f"{'s' if len(blocked) != 1 else ''}:",
        "",
    ]
    for cc in blocked:
        lines.append(f"  [blocked] {cc.kind} \"{cc.content_name}\"")
        if cc.kind == "modified" and cc.field_changes:
            blocked_fcs = [
                fc for fc in cc.field_changes
                if _classification_for_field_change(fc, cc) == "blocked"
            ]
            for fc in blocked_fcs:
                lines.append(
                    f"      field \"{fc.field_name}\": {fc.kind} "
                    f"({_describe_detail(fc.detail)})"
                )
    lines.append("")
    lines.append(
        "Blocked changes cannot be acknowledged — they would lose "
        "data or break invariants. Reshape the IR or migrate the "
        "data manually before retrying."
    )
    return "\n".join(lines)


def format_unacked_error(
    diff: MigrationDiff, migrations_config: Mapping[str, Any],
) -> str:
    missing = missing_acks(diff, migrations_config)
    lines = [
        f"Termin migration refused — {len(missing)} risky change"
        f"{'s' if len(missing) != 1 else ''} need explicit "
        f"acknowledgment:",
        "",
    ]
    for fp in missing:
        lines.append(f"  [{_tier_for_fingerprint(diff, fp)}] {fp}")
    lines.append("")
    lines.append(
        "Add the fingerprints to your deploy config:"
    )
    lines.append("")
    lines.append("  migrations:")
    lines.append("    accepted_changes:")
    for fp in missing:
        lines.append(f"      - \"{fp}\"")
    lines.append("")
    # If any of the missing changes are low-tier, mention the
    # dev-mode blanket as an option. Medium/high tiers always require
    # per-change ack regardless of dev_mode, so we do not advertise
    # the flag for those.
    has_low = any(
        _tier_for_fingerprint(diff, fp) == "low" for fp in missing
    )
    if has_low:
        lines.append(
            "For low-tier changes only, you may instead set both "
            "migrations.dev_mode: true and migrations.accept_any_low: "
            "true (developer-convenience; refused in production)."
        )
    return "\n".join(lines)


def _classification_for_field_change(fc: FieldChange, cc: ContentChange) -> str:
    """Best-effort lookup of a field-change's tier given its parent
    ContentChange. Used only for error formatting; not load-bearing."""
    from .classifier import classify_field_change
    target_fields = {f["name"]: f for f in (cc.schema or {}).get("fields", [])}
    spec = target_fields.get(fc.field_name)
    return classify_field_change(fc, field_spec=spec)


def _tier_for_fingerprint(diff: MigrationDiff, fp: str) -> str:
    """Walk the diff to find the tier for a fingerprint string.
    Returns 'unknown' if not found (shouldn't happen but defensive)."""
    for cc in diff.changes:
        if cc.classification in ("safe", "blocked"):
            continue
        if cc.kind == "modified":
            for fc in cc.field_changes:
                if fingerprint_change(fc, content_name=cc.content_name) == fp:
                    return _classification_for_field_change(fc, cc)
        else:
            if fingerprint_change(cc) == fp:
                return cc.classification
    return "unknown"


def _describe_detail(detail: Mapping[str, Any]) -> str:
    if not detail:
        return ""
    parts = []
    for k, v in sorted(detail.items()):
        if k == "spec":
            continue
        parts.append(f"{k}={v!r}")
    return ", ".join(parts)
