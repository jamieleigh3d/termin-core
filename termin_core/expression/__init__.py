# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Expression evaluation surface — server-side CEL evaluator and the
Predicate-AST compiler used by the storage contract.

* ``ExpressionEvaluator`` (``cel`` module) — the runtime CEL
  evaluator wrapping cel-python with Termin's helper functions
  (sum/avg/min/max/days-between/etc.).
* ``compile_cel_to_predicate`` (``predicate`` module) — compiles a
  subset of CEL expressions into the structural Predicate AST in
  ``termin_core.providers.storage_contract``, used by storage
  providers to translate CEL filter expressions into native query
  predicates.
"""

from .cel import ExpressionEvaluator  # noqa: F401
from .predicate import (  # noqa: F401
    NotCompilable,
    compile_cel_to_predicate,
)

__all__ = [
    "ExpressionEvaluator",
    "NotCompilable",
    "compile_cel_to_predicate",
]
