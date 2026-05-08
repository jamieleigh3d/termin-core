# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Migration-related error types and TERMIN-M code allocations.

Per docs/migration-classifier-design.md §8.

  TERMIN-M001  blocked migration (data loss path)
  TERMIN-M002  unack'd low/medium/high risk migration
  TERMIN-M003  validation step failed post-migration
  TERMIN-M004  backup creation failed or refused
  TERMIN-M005  rename mapping cycle / conflict
  TERMIN-M006  rename mapping target doesn't match IR shape
"""


class _MigrationError(RuntimeError):
    """Base class for runtime-side migration errors. Provider-side
    errors (BackupFailedError, MigrationValidationError) live in
    storage_contract.py — those raise from inside the provider, not
    the runtime classifier/orchestrator."""
    code: str = "TERMIN-M000"


class MigrationBlockedError(_MigrationError):
    """The diff has at least one blocked change. The deploy refuses
    unconditionally; the operator must reshape the IR or the data
    manually before retrying."""
    code = "TERMIN-M001"


class MigrationAckRequiredError(_MigrationError):
    """The diff has at least one low/medium/high risk change that
    the deploy config has not acknowledged. The operator must add
    the missing fingerprint(s) to migrations.accepted_changes. For
    low-tier changes only, setting both migrations.dev_mode=true and
    migrations.accept_any_low=true is also accepted (developer
    convenience; refused in production)."""
    code = "TERMIN-M002"


class MigrationBackupRefusedError(_MigrationError):
    """A high-risk migration required a backup but the provider
    returned None from create_backup() — meaning the provider
    cannot back up in its current configuration. The operator
    should back up externally (filesystem snapshot, cloud snapshot,
    pg_dump, etc.) before retrying."""
    code = "TERMIN-M004"


class RenameMappingError(_MigrationError):
    """The deploy config's rename mapping is internally
    inconsistent (cycle, duplicate target, target doesn't match
    IR shape). Sub-codes:
      - M005: cycle or duplicate target
      - M006: target doesn't match IR shape
    """
    def __init__(self, message: str, sub_code: str = "TERMIN-M005") -> None:
        super().__init__(message)
        self.code = sub_code
