# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Storage contract surface — v0.9 Phase 2.

Per BRD §6.2, Storage is a single contract surface (category=storage,
name="default") with these operations:

    create(content_type, record, idempotency_key?) -> record
    read(content_type, id) -> record | null
    query(content_type, predicate, options) -> Page<record>
    update(content_type, id, patch) -> record
    delete(content_type, id, cascade_mode) -> bool
    migrate(schema_diff) -> void  (admin-only, runtime-managed)

This module declares the typed shapes (Predicate AST, QueryOptions,
Page, CascadeMode, MigrationDiff) and the Protocol every storage
provider must satisfy. Concrete providers (sqlite, postgres-future,
dynamo-future, etc.) live in termin_runtime/providers/builtins/ or
in third-party packages.

Provider boundary discipline (BRD §6.2 "Provider's job is small"):
  - The provider does SQL/persistence and nothing else.
  - Event publishing, error routing through TerminAtor, HTTP status
    code translation, and confidentiality redaction are RUNTIME
    concerns, not provider concerns. The runtime calls the provider,
    interprets the result, and then publishes events / raises 404 /
    redacts fields itself.
  - This separation is what makes the contract portable. Postgres
    and DynamoDB providers will not understand TerminAtor or
    FastAPI's HTTPException; they should not have to.

Predicate AST (BRD §6.2):
  Eq, Ne, Gt, Gte, Lt, Lte, In, Contains, And, Or, Not.
  Providers implement the AST; they do NOT implement CEL. The
  runtime compiles source-level CEL down to the AST where possible
  and evaluates any non-pushable residual in-process. One CEL
  evaluator (cel-python) lives in the runtime.

Cascade semantics (BRD §6.2):
  delete() takes an explicit cascade_mode: "cascade" | "restrict".
  v0.9 grammar requires every `references X` to declare cascade
  behavior at content-definition time; the runtime resolves the
  per-reference declaration into the cascade_mode passed to the
  provider. A bare `references X` is a parse error in v0.9.
  Restrict is the safe default at the contract level for callers
  that don't supply a cascade resolution (e.g. test scaffolding).

Schema migration (BRD §6.2):
  migrate(diff) is called at deploy time after the runtime has
  classified each change as Safe / Risky / Blocked. The provider
  applies the diff in a single transaction and rolls back on any
  failure. v0.9 ships with the contract surface complete; the
  runtime's diff classifier is a Phase 2 follow-on item — see
  classify_migration_diff() below for the placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence, Union, runtime_checkable


# ── Predicate AST ──
#
# Closed sum type. Providers pattern-match on the dataclass identity
# (using isinstance) and produce backend-native query fragments. The
# tree is immutable (frozen=True) so callers can safely cache compiled
# predicates.
#
# Field references are unqualified strings (column names within one
# content type). Cross-content joins are not part of the v0.9
# predicate language — predicates always run against a single
# content_type passed alongside.


@dataclass(frozen=True)
class Eq:
    """field == value"""
    field: str
    value: Any


@dataclass(frozen=True)
class Ne:
    """field != value"""
    field: str
    value: Any


@dataclass(frozen=True)
class Gt:
    """field > value"""
    field: str
    value: Any


@dataclass(frozen=True)
class Gte:
    """field >= value"""
    field: str
    value: Any


@dataclass(frozen=True)
class Lt:
    """field < value"""
    field: str
    value: Any


@dataclass(frozen=True)
class Lte:
    """field <= value"""
    field: str
    value: Any


@dataclass(frozen=True)
class In:
    """field in values (membership)"""
    field: str
    values: tuple

    def __post_init__(self) -> None:
        # Coerce list/set to tuple so the dataclass stays hashable.
        if not isinstance(self.values, tuple):
            object.__setattr__(self, "values", tuple(self.values))


@dataclass(frozen=True)
class Contains:
    """Case-sensitive substring match (BRD §6.2)."""
    field: str
    substring: str


@dataclass(frozen=True)
class And:
    """Conjunction of sub-predicates."""
    predicates: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.predicates, tuple):
            object.__setattr__(self, "predicates", tuple(self.predicates))
        if not self.predicates:
            raise ValueError("And requires at least one predicate")


@dataclass(frozen=True)
class Or:
    """Disjunction of sub-predicates."""
    predicates: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.predicates, tuple):
            object.__setattr__(self, "predicates", tuple(self.predicates))
        if not self.predicates:
            raise ValueError("Or requires at least one predicate")


@dataclass(frozen=True)
class Not:
    """Negation."""
    predicate: "Predicate"


# Sum type for static-checking callers and for downstream pattern
# matching. Providers should accept a Predicate and pattern-match on
# the dataclass type.
Predicate = Union[Eq, Ne, Gt, Gte, Lt, Lte, In, Contains, And, Or, Not]


# ── Query options ──


@dataclass(frozen=True)
class OrderBy:
    """One sort key. direction is 'asc' | 'desc' (lowercase)."""
    field: str
    direction: str = "asc"

    def __post_init__(self) -> None:
        if self.direction not in ("asc", "desc"):
            raise ValueError(
                f"OrderBy.direction must be 'asc' | 'desc', got {self.direction!r}"
            )


@dataclass(frozen=True)
class QueryOptions:
    """Pagination + sort options for query().

    BRD §6.2: cursor-based pagination, no offset. v0.9 ships an
    opaque-cursor convention; providers may encode the cursor in
    whatever form they like (the runtime treats it as opaque).

    limit defaults to 50, max 1000. order_by defaults to empty;
    if the supplied order_by doesn't include a unique field the
    runtime appends `id` as a final tiebreaker for sort stability.
    """
    limit: int = 50
    cursor: Optional[str] = None
    order_by: tuple = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.limit, int) or self.limit < 0:
            raise ValueError(
                f"QueryOptions.limit must be a non-negative int, got {self.limit!r}"
            )
        if self.limit > 1000:
            raise ValueError(
                f"QueryOptions.limit must not exceed 1000, got {self.limit}"
            )
        if not isinstance(self.order_by, tuple):
            object.__setattr__(self, "order_by", tuple(self.order_by))
        for ob in self.order_by:
            if not isinstance(ob, OrderBy):
                raise TypeError(
                    f"QueryOptions.order_by entries must be OrderBy, got {type(ob).__name__}"
                )


@dataclass(frozen=True)
class Page:
    """One page of query results.

    next_cursor is None when no further pages exist. estimated_total
    is provider-optional — None means the provider didn't supply a
    count (callers must not assume zero from None).
    """
    records: tuple
    next_cursor: Optional[str] = None
    estimated_total: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple):
            object.__setattr__(self, "records", tuple(self.records))


# ── Cascade modes ──


class CascadeMode(str, Enum):
    """Per-reference cascade declaration (BRD §6.2).

    String-valued so it serializes cleanly in IR and audit records.

    CASCADE: deleting the referenced row also deletes referrers.
    RESTRICT: deleting the referenced row fails if any referrers
              exist (the safe default at the SQL level).

    v0.9 grammar requires explicit declaration at the source level —
    a bare `references X` line is a parse error. The runtime
    resolves the per-reference declaration into the mode passed to
    delete().
    """
    CASCADE = "cascade"
    RESTRICT = "restrict"


# ── Conditional-update result ──


@dataclass(frozen=True)
class UpdateResult:
    """Outcome of a conditional update (StorageProvider.update_if).

    Three terminal states distinguish the cases that callers route
    differently:

      - applied=True, record=<post-update> — update went through.
      - applied=False, record=None, reason="not_found" — id didn't
        match any row. Runtime translates to HTTP 404.
      - applied=False, record=<current>, reason="condition_failed" —
        row exists but the predicate didn't match the current state.
        Runtime translates to HTTP 409 and surfaces the current
        record so the UI can show "someone else already changed it".

    Attributes:
      applied: True iff the update was applied.
      record:  The post-update record if applied; the pre-update
               record if condition_failed; None if not_found.
      reason:  "applied" | "not_found" | "condition_failed".
    """
    applied: bool
    record: Optional[Mapping[str, Any]]
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in ("applied", "not_found", "condition_failed"):
            raise ValueError(
                f"UpdateResult.reason must be 'applied' | 'not_found' | "
                f"'condition_failed', got {self.reason!r}")


# ── Migration errors ──


class BackupFailedError(RuntimeError):
    """Provider attempted to create a backup and failed (disk full,
    permission denied, integrity_check failed, etc.). The runtime
    translates this to TERMIN-M004 and refuses the migration."""


class MigrationValidationError(RuntimeError):
    """Provider's internal validation step failed after applying the
    diff but before COMMIT. The transaction has been rolled back; if
    a backup was created, the operator can restore from it. The
    runtime translates this to TERMIN-M003.

    Attributes:
        failures: tuple of human-readable failure descriptions.
        backup_id: optional operator-visible backup identifier
                   (set by the runtime after the provider raises).
    """
    def __init__(self, failures, backup_id: Optional[str] = None) -> None:
        self.failures = tuple(failures)
        self.backup_id = backup_id
        msg = "Migration validation failed: " + "; ".join(self.failures)
        if backup_id:
            msg += f" (recovery: {backup_id})"
        super().__init__(msg)


class ProviderInjectedFault(RuntimeError):
    """Test-only exception raised by a provider's fault injection
    hook (`StorageProvider._inject_fault_at`). Conformance hook per
    migration-contract.md §6.2 + §9.4.

    Carries the canonical stage name. The conformance pack uses this
    to verify atomicity: a fault injected at any stage during
    migrate() must leave the database observably identical to its
    pre-call state.

    Stages:
      - "pre_apply"  — before any change has been applied
      - "mid_apply"  — after applying the first change but before all
      - "pre_commit" — after all changes are applied but before
                       the transaction commits
    """
    def __init__(self, stage: str) -> None:
        super().__init__(f"injected fault at stage: {stage}")
        self.stage = stage


# ── Schema migration ──
#
# Phase 2 ships the contract surface; the runtime's diff classifier
# is a follow-on. The dataclasses below describe the diff shape so
# providers can implement migrate() against a stable type.


# Phase 2.x (b) — five-tier classification per
# docs/migration-classifier-design.md §3.12.1.
#
# Worst-to-best: blocked > high > medium > low > safe.
# - blocked: refuse the deploy.
# - high:    rebuild + data semantics shift (or FK integrity briefly
#            broken). Backup + validation step + ack required.
# - medium:  rebuild but data preserved. Validation + ack required.
# - low:     in-place ALTER, easily reversible. Ack required.
# - safe:    no operator interaction.
CLASSIFICATIONS: tuple = ("safe", "low", "medium", "high", "blocked")
_CLASS_RANK: dict = {c: i for i, c in enumerate(CLASSIFICATIONS)}


def worst_classification(*classifications: str) -> str:
    """Return the worst (rightmost) classification per CLASSIFICATIONS
    ordering. Any unknown value raises ValueError."""
    if not classifications:
        return "safe"
    for c in classifications:
        if c not in _CLASS_RANK:
            raise ValueError(
                f"Unknown classification: {c!r}. Must be one of "
                f"{CLASSIFICATIONS}.")
    return max(classifications, key=lambda c: _CLASS_RANK[c])


# Allowed FieldChange.kind values per §3.10 + §3.13 (rename support).
# Each kind carries a structured `detail` dict whose shape depends
# on the kind; classifier relies on this granularity to emit
# the correct tier per §3.3.
_FIELD_CHANGE_KINDS: tuple = (
    "added",                  # detail = {"spec": <full FieldSpec dict>}
    "removed",                # detail = {} (field name is enough)
    "renamed",                # detail = {"from": str, "to": str, "type_changed": bool}
    "type_changed",           # detail = {"from_type": str, "to_type": str}
    "required_added",         # detail = {}
    "required_removed",       # detail = {}
    "unique_added",           # detail = {}
    "unique_removed",         # detail = {}
    "bounds_changed",         # detail = {"from": {min, max}, "to": {min, max}, "tightening": bool}
    "enum_values_changed",    # detail = {"added": [..], "removed": [..]}
    "cascade_mode_changed",   # detail = {"from": str | None, "to": str}
    "foreign_key_changed",    # detail = {"from": str | None, "to": str | None}
)


@dataclass(frozen=True)
class FieldChange:
    """One field-level change within a content schema modification.

    Phase 2.x (b) widens kind from the original 4-state enum to ~12
    granular kinds so the classifier can emit the right tier per
    docs/migration-classifier-design.md §3.3.
    """
    kind: str
    field_name: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _FIELD_CHANGE_KINDS:
            raise ValueError(
                f"FieldChange.kind must be one of {_FIELD_CHANGE_KINDS}, "
                f"got {self.kind!r}"
            )


# Allowed ContentChange.kind values. Phase 2.x (b) adds "renamed"
# per §3.13 so operator-declared rename mappings can fold a
# remove+add pair into a single entry without losing the data.
_CONTENT_CHANGE_KINDS: tuple = ("added", "removed", "modified", "renamed")


@dataclass(frozen=True)
class ContentChange:
    """One content-level change in the migration diff."""
    kind: str
    content_name: str
    classification: str
    schema: Optional[Mapping[str, Any]] = None  # full schema for added/modified/renamed
    field_changes: tuple = field(default_factory=tuple)
    detail: Mapping[str, Any] = field(default_factory=dict)
    # detail shape per kind:
    #   "added"    : {} (schema carries everything)
    #   "removed"  : {} (content_name is enough)
    #   "modified" : {} (field_changes carry the deltas)
    #   "renamed"  : {"from": str}  (content_name is the new name)

    def __post_init__(self) -> None:
        if self.kind not in _CONTENT_CHANGE_KINDS:
            raise ValueError(
                f"ContentChange.kind must be one of {_CONTENT_CHANGE_KINDS}, "
                f"got {self.kind!r}"
            )
        if self.classification not in CLASSIFICATIONS:
            raise ValueError(
                f"ContentChange.classification must be one of "
                f"{CLASSIFICATIONS}, got {self.classification!r}"
            )
        if not isinstance(self.field_changes, tuple):
            object.__setattr__(self, "field_changes", tuple(self.field_changes))


@dataclass(frozen=True)
class MigrationDiff:
    """The argument to provider.migrate().

    Initial deploy is modeled as a diff with all content schemas in
    `changes` as "added" entries — the same code path handles both
    first-deploy and subsequent migrations. The runtime is
    responsible for classifying each change before passing the diff
    to the provider; providers do not classify.
    """
    changes: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.changes, tuple):
            object.__setattr__(self, "changes", tuple(self.changes))

    @property
    def is_blocked(self) -> bool:
        """True iff any change is classified blocked. Runtime should
        refuse to invoke migrate() on a blocked diff."""
        return any(c.classification == "blocked" for c in self.changes)

    @property
    def has_high_risk(self) -> bool:
        """True iff any change is classified high risk. Runtime
        requires operator ack AND triggers a backup before applying."""
        return any(c.classification == "high" for c in self.changes)

    @property
    def has_medium_risk(self) -> bool:
        """True iff any change is classified medium risk. Runtime
        requires operator ack."""
        return any(c.classification == "medium" for c in self.changes)

    @property
    def has_low_risk(self) -> bool:
        """True iff any change is classified low risk. Runtime
        requires operator ack."""
        return any(c.classification == "low" for c in self.changes)

    @property
    def needs_ack(self) -> bool:
        """True iff any change is classified low/medium/high risk.
        Runtime requires the deploy config to acknowledge the
        non-safe portion of the diff before applying."""
        return self.has_low_risk or self.has_medium_risk or self.has_high_risk

    @property
    def has_risky(self) -> bool:
        """Backwards-compat shim — pre-2.x (b) callers used a single
        `risky` tier. Returns True iff the diff has any non-safe,
        non-blocked change."""
        return self.needs_ack

    @property
    def overall_classification(self) -> str:
        """Worst classification across all changes; "safe" if empty
        diff."""
        if not self.changes:
            return "safe"
        return worst_classification(*(c.classification for c in self.changes))


# ── Storage contract Protocol ──


@runtime_checkable
class StorageProvider(Protocol):
    """The Storage contract surface (BRD §6.2).

    Providers implement this Protocol. The runtime never reads or
    writes content storage directly — every CRUD path goes through a
    provider instance held on RuntimeContext.storage.

    Provider boundary discipline:
      - Pure storage ops only. No event publishing, no error
        routing, no HTTP status codes, no redaction.
      - Errors raised by the provider are domain-typed: ValueError
        for caller mistakes (unknown content type, malformed
        predicate); IntegrityError-equivalents (provider-defined)
        for referential integrity violations; pass-through for
        underlying-driver exceptions.
      - The runtime catches and translates these into HTTP responses,
        TerminAtor errors, and event-bus publishes.

    Lifecycle:
      - The factory function constructs a provider instance from
        deploy config (e.g., the SQLite provider's config has the
        db_path; a future Postgres provider's config has the DSN).
      - The runtime calls migrate() during app startup with the
        initial-deploy or evolved-deploy diff.
      - The runtime calls CRUD operations during request handling.
      - The provider may hold connection pools, prepared statements,
        etc.; the lifecycle is bounded by the FastAPI lifespan.
    """

    # ── Lifecycle ──

    async def migrate(self, diff: MigrationDiff) -> None:
        """Apply a schema migration.

        Called by the runtime at app startup. For a fresh deploy
        the diff contains all content schemas as "added" entries.
        Providers MUST apply the diff in a single transaction and
        roll back on any failure; partial migrations are a contract
        violation.

        Raises if any change is classified "blocked" — providers
        defer that judgment to the runtime, but should defensively
        refuse a blocked diff to catch runtime bugs.

        Phase 2.x (b): Providers now implement modify/remove/rename
        paths in addition to add. The runtime's classifier ensures
        the diff arrives with each change tagged at the appropriate
        tier (safe/low/medium/high); the provider applies what it
        is told. Provider runs an internal validation step before
        committing the transaction (FK integrity, row-count
        preservation for rebuilt tables, schema metadata
        round-trip). If validation fails, the transaction is rolled
        back and a MigrationValidationError is raised — the runtime
        translates that to TERMIN-M003.
        """
        ...

    async def read_schema_metadata(self) -> Optional[Mapping[str, Any]]:
        """Return the last-stored schema (the IR `content` list) for
        this provider, or None if no metadata has ever been written.

        Used by the runtime to compute (current → target) migration
        diffs at startup. On first-ever-v0.9 boot against a v0.8
        database, the provider may also fall back to schema
        introspection (sqlite_master, PRAGMA table_info) if the
        metadata table doesn't exist — the runtime is agnostic to
        the source.

        Returns None on a brand-new deployment (empty database with
        no tables and no metadata). The runtime then runs an
        initial-deploy migration that creates everything.
        """
        ...

    async def write_schema_metadata(
        self, content_schemas: Sequence[Mapping[str, Any]]
    ) -> None:
        """Persist the just-applied schema as the new
        last-known-good. Called by the runtime after migrate()
        succeeds and validation passes. The provider stores it in
        whatever shape it prefers (a metadata table for SQL
        backends, an item in a control-plane partition for
        DynamoDB, etc.); read_schema_metadata returns it on the
        next boot.
        """
        ...

    def _inject_fault_at(self, stage: Optional[str]) -> None:
        """Test-only: arm the provider to raise ProviderInjectedFault
        at the named stage on the NEXT migrate() call.

        Conformance hook per migration-contract.md §6.2 + §9.4.
        Conforming providers honor this to support the conformance
        test pack's atomicity / fault-injection tests. Production
        providers may treat this as a no-op when the runtime is not
        in a test mode.

        Stages (canonical, language-level):
          - "pre_apply"  — raise before any change has been applied;
            DB must be observably identical to its pre-call state.
          - "mid_apply"  — raise after applying the first change but
            before applying all; the partial work must be rolled back.
          - "pre_commit" — raise after all changes are applied but
            before the transaction commits; the apply must be rolled
            back atomically.

        Pass None to disarm (clear any previously-armed stage). The
        flag is one-shot: it is cleared automatically when migrate()
        observes it, so a subsequent migrate() runs unfaulted unless
        re-armed.
        """
        ...

    async def create_backup(self) -> Optional[str]:
        """Create a backup of the storage state, suitable for
        recovering from a failed high-risk migration (per
        docs/migration-classifier-design.md §3.12.2).

        Returns an operator-visible identifier for the backup (a
        file path for SQLite, a snapshot ARN for cloud DBs, etc.).
        The runtime surfaces this identifier in startup-log lines
        and migration-failure errors so the operator can find the
        recovery path.

        Returns None if this provider cannot create a backup in the
        current configuration. The runtime treats None as a
        fail-closed signal: high-risk migrations refuse to proceed.
        Operators must back up externally before retrying.

        Raises BackupFailedError on attempted-but-failed backup
        (disk full, permission denied, etc.); the runtime translates
        that to TERMIN-M004.
        """
        ...

    # ── CRUD ──

    async def create(
        self,
        content_type: str,
        record: Mapping[str, Any],
        *,
        idempotency_key: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """Insert a new record. Returns the persisted record with id.

        idempotency_key (BRD §6.2): if supplied, second call with
        same key is a silent no-op returning the original record.
        v0.9 first-party SQLite provider does not yet implement
        idempotency-key dedup (Phase 2 follow-on); callers may pass
        the key but the provider currently ignores it. The contract
        surface is stable; the impl will catch up.
        """
        ...

    async def read(
        self, content_type: str, id: Any
    ) -> Optional[Mapping[str, Any]]:
        """Fetch a single record by primary key. None if not found.

        Per BRD §6.2 the contract returns None — HTTP 404 is a
        runtime translation, not a provider responsibility.
        """
        ...

    async def query(
        self,
        content_type: str,
        predicate: Optional[Predicate] = None,
        options: Optional[QueryOptions] = None,
    ) -> Page:
        """Run a structured query.

        predicate=None matches all records. options=None uses
        defaults (limit=50, no cursor, no order_by — provider
        appends id for sort stability).

        Returns a Page; next_cursor is None when no further pages
        exist.
        """
        ...

    async def update(
        self,
        content_type: str,
        id: Any,
        patch: Mapping[str, Any],
    ) -> Optional[Mapping[str, Any]]:
        """Update fields on an existing record. Returns the post-update
        record, or None if the record didn't exist.

        Patch semantics: keys present in `patch` overwrite; absent
        keys are unchanged. To clear a field, pass an explicit None
        (not omitted).
        """
        ...

    async def update_if(
        self,
        content_type: str,
        id: Any,
        condition: "Predicate",
        patch: Mapping[str, Any],
    ) -> UpdateResult:
        """Conditional ("compare-and-set") update.

        Applies `patch` to the record at `id` ONLY IF the record's
        current state matches `condition` (any Predicate AST node —
        Eq, Ne, Gt, And, Or, Not, etc.). Atomic per-call: providers
        MUST ensure the read-and-write is serializable, either by
        pushing the predicate down to a single SQL UPDATE WHERE
        statement (the SqliteStorageProvider strategy) or by
        wrapping in a transaction with appropriate locking.

        Returns UpdateResult with three terminal states:
          - applied=True with the post-update record
          - applied=False, reason="not_found" (id didn't match)
          - applied=False, reason="condition_failed" with the
            current record so the caller can show what blocked it

        Use cases (BRD §6.2):
          - State-machine transitions: condition = Eq("status", "draft"),
            patch = {"status": "in_review"}.
          - Optimistic concurrency: condition = Eq("version", 5),
            patch = {"version": 6, ...changes...}.
          - Claim-only-if-unclaimed: condition = Eq("assignee", None),
            patch = {"assignee": user_id}.

        v0.9 Phase 2.x (c) ships idempotency_key on create() only;
        update_if's predicate provides the equivalent
        optimistic-concurrency guarantee for updates without needing
        a separate idempotency surface.
        """
        ...

    async def delete(
        self,
        content_type: str,
        id: Any,
        cascade_mode: CascadeMode = CascadeMode.RESTRICT,
    ) -> bool:
        """Delete a record. Returns True on success, False if no row
        existed at that id.

        cascade_mode controls referential cleanup:
          CASCADE: also delete referrers (per the source-declared
            cascade direction). The provider walks the FK graph in
            the right order.
          RESTRICT: refuse the delete if any referrer exists. The
            provider raises a referential-integrity error (subclass
            varies by backend; SQLite raises IntegrityError).

        Per BRD §6.2 the contract caller (runtime) is responsible
        for blast-radius computation and operator confirmation
        before requesting CASCADE; providers obey the supplied mode.
        """
        ...


# ── Convenience: classify a fresh deploy as a "create-everything" diff ──


def initial_deploy_diff(content_schemas: Sequence[Mapping[str, Any]]) -> MigrationDiff:
    """Build a MigrationDiff that creates every content type fresh.

    Used at first deploy. All changes are classified "safe" because
    no existing data can be invalidated by adding new content types.
    """
    changes = tuple(
        ContentChange(
            kind="added",
            content_name=cs["name"]["snake"],
            classification="safe",
            schema=cs,
        )
        for cs in content_schemas
    )
    return MigrationDiff(changes=changes)
