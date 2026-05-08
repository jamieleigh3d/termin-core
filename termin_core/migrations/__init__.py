# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""v0.9 Phase 2.x (b) — migration diff classifier.

Per docs/migration-classifier-design.md, this package owns the
runtime's responsibility for:

  - Computing a migration diff from (current_schema, target_schemas).
  - Classifying each change in the diff as one of
    safe / low / medium / high / blocked.
  - Folding operator-declared rename mappings (deploy config) so
    remove+add pairs collapse into single "renamed" changes — data
    is preserved through SQL-level RENAME COLUMN / RENAME TO.
  - Downgrading classification for empty-table changes (low, not
    safe, per JL's review).
  - Fingerprinting changes for per-change operator acknowledgment
    via deploy config.
  - Validation step (post-apply, pre-COMMIT) checking FK integrity,
    row-count preservation, schema metadata round-trip, smoke reads.

Backup is a *provider* responsibility (storage_contract Protocol):
different backends have different primitives. The runtime asks the
provider to back up before any high-risk migration; if the provider
returns None ("cannot back up"), the migration refuses.

Public surface:
  - compute_migration_diff(current, target) → MigrationDiff (pure)
  - apply_rename_mappings(diff, mappings) → MigrationDiff (pure)
  - classify_field_change(change, ...) → str (pure)
  - classify_content_change(change) → str (pure)
  - downgrade_for_empty_tables(diff, provider) → MigrationDiff (async)
  - fingerprint_change(change) → str (pure)
  - ack_covers(diff, deploy_config) → bool (pure)
  - run_validation(provider, diff, target) → ValidationResult (async)

See termin_runtime/providers/storage_contract.py for the typed
shapes (FieldChange, ContentChange, MigrationDiff).
"""

from .classifier import (
    compute_migration_diff,
    apply_rename_mappings,
    classify_field_change,
    classify_content_change,
    downgrade_for_empty_tables,
)
from .ack import (
    fingerprint_change,
    ack_covers,
    format_blocked_error,
    format_unacked_error,
)
from .validate import (
    ValidationResult,
    run_validation,
)
from .errors import (
    MigrationBlockedError,
    MigrationAckRequiredError,
    MigrationBackupRefusedError,
    RenameMappingError,
)

__all__ = [
    "compute_migration_diff",
    "apply_rename_mappings",
    "classify_field_change",
    "classify_content_change",
    "downgrade_for_empty_tables",
    "fingerprint_change",
    "ack_covers",
    "format_blocked_error",
    "format_unacked_error",
    "ValidationResult",
    "run_validation",
    "MigrationBlockedError",
    "MigrationAckRequiredError",
    "MigrationBackupRefusedError",
    "RenameMappingError",
]
