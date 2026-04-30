# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Error model for Termin runtimes.

* ``TerminError`` — the structured error envelope every runtime
  emits when an operation fails (validation, scope check, FK
  violation, state-machine refusal, etc.).
* ``TerminAtor`` — the in-runtime error router that captures errors,
  enriches them with context, and dispatches to event subscribers.

Pure value types and routing logic. No framework dependency.
"""

from .router import TerminError, TerminAtor  # noqa: F401

__all__ = ["TerminError", "TerminAtor"]
