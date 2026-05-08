# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Post-migration validation step.

Per docs/migration-classifier-design.md §3.12.3.

After the provider has applied a diff and is about to COMMIT, the
runtime asks it to run an automated validation step. If anything
fails, the transaction rolls back; if a backup was created, the
operator is told to recover from it.

This module exposes a uniform shape; the SqliteStorageProvider
implements the actual checks via internal helpers it owns. Other
providers (Postgres, DynamoDB) implement their own equivalents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of the post-migration validation step.

    Attributes:
      ok: True iff every check passed.
      failures: tuple of human-readable failure descriptions.
                Empty when ok is True.
    """
    ok: bool
    failures: tuple

    @classmethod
    def passing(cls) -> "ValidationResult":
        return cls(ok=True, failures=())

    @classmethod
    def failing(cls, *messages: str) -> "ValidationResult":
        return cls(ok=False, failures=tuple(messages))


async def run_validation(
    provider, diff, target_schemas: Sequence,
) -> ValidationResult:
    """Run the validation step against `provider` for the given
    `diff` and `target_schemas`.

    Currently delegates entirely to the provider's internal
    `_run_post_migration_validation` method, if present. v0.9 keeps
    custom-validator extensibility unspecified (v1.0 backlog).
    """
    impl = getattr(provider, "_run_post_migration_validation", None)
    if impl is None:
        return ValidationResult.passing()
    return await impl(diff, target_schemas)
