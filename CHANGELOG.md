# Changelog

## Unreleased — v0.9 in progress (feature/v0.9)

### Slice 7.1 — pure types and Protocols extracted (2026-04-30)

The `termin-core` repo opens with the framework-free contract surface
that any conforming Termin runtime imports. Six target subtrees moved
from `termin-compiler/termin_runtime/` and `termin-compiler/termin/`:

| Subpackage | Source | Lines |
|---|---|---|
| `termin_core.providers` | `termin_runtime/providers/{contracts,registry,binding,deploy_config,*_contract}.py` | ~2300 |
| `termin_core.ir` | `termin/{ir,ir_serialize}.py` | ~600 |
| `termin_core.expression` | `termin_runtime/{expression,cel_predicate}.py` | ~620 |
| `termin_core.confidentiality` | `termin_runtime/confidentiality.py` | ~190 |
| `termin_core.errors` | `termin_runtime/errors.py` | ~110 |

`Principal` and `PrincipalContext` value types travel with the
provider Protocols (they live in `identity_contract.py` alongside
the `IdentityProvider` Protocol they describe) — folded into the
provider move rather than premature-refactoring into a separate
subpackage.

**Smoke-test guard:** `tests/test_smoke.py::test_no_fastapi_dependency`
verifies the import graph cannot silently regrow a transitive
dependency on FastAPI / uvicorn / aiosqlite / Anthropic / Jinja2.
The whole point of the extraction is that alternate runtimes get the
contract surface without those deps; the guard locks it in.

**Deferred to slice 7.2:**

- `termin_runtime/validation.py` — D-19 dependent_values + one_of
  validators raise `fastapi.HTTPException` directly. Clean extraction
  needs the framework-agnostic exception story slice 7.2 introduces
  (`TerminRequest`/`TerminResponse` abstractions, ASGI substrate).
- `termin_runtime/state.py` and `transitions.py` — pure
  state-machine rules are mixed with HTTPException-raising orchestration
  and storage-write side effects. Same blocker as validation.

Both deferred items get extracted in slice 7.2 once `termin-core`
ships a `TerminValidationError` / `TerminTransitionError` exception
type that adapters translate to their framework's error envelope.

**Reference runtime is unchanged behaviorally.** Every file in
`termin-compiler/termin_runtime/providers/` (except `builtins/`) and
the moved-out files in `termin-compiler/termin/{ir,ir_serialize}.py`
and `termin-compiler/termin_runtime/{expression,cel_predicate,confidentiality,errors}.py`
become re-export shims for v0.9. Existing
`from termin_runtime.X import Y` imports continue working through
the shim layer; slice 7.5 of Phase 7 drops the shims after a
deprecation pass.

**Suites:** compiler 2545 passing on Windows (no behavior change);
conformance 915 passing on Windows.
