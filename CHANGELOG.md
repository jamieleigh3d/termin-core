# Changelog

## [0.9.1] — 2026-05-01

A correctness + hygiene patch release on top of v0.9.0. No IR
schema changes; no breaking surface changes. The contract Protocols,
IR types, and routing-dispatch surface are byte-identical to v0.9.0
plus the additions below.

### Added

- **`make_anonymous_principal(session_marker)`** factory in
  `providers/identity_contract.py`. Anonymous is now a *type* with
  a stable auditable id of the form `anonymous:<sanitized-marker>`
  rather than an empty-string null. The runtime calls this at the
  identity-resolution layer (in `termin-server`'s
  `_resolve_principal_and_scopes`), deriving the session marker
  from the `termin_user_name` cookie. Operators can filter audit
  logs with `invoked_by_principal_id LIKE 'anonymous:%'` to find
  anonymous-caller activity.
- `Principal.is_anonymous` now recognizes both the canonical
  sentinel id and the new typed `anonymous:<marker>` form.
- `routing/compute.py::trigger_compute_handler` now reads
  `request.auth.principal` and forwards it as the `invoked_by`
  kwarg to `ctx.execute_compute(...)`. This was the load-bearing
  fix that made manual-trigger CEL audit rows stamp the right
  principal columns per BRD §6.3.4. The matching runtime change
  lives in `termin-server`'s `compute_runner._execute_cel_compute`.

### Changed

- Renamed `queue-and-retry-forever` → `queue-and-retry` in the
  `failure_mode` comment on `ChannelSpec` (`ir/types.py`) and the
  `ChannelProvider` Protocol module docstring
  (`providers/channel_contract.py`). The "forever" qualifier was
  operationally wrong — the v0.10 retry-worker design caps retry
  duration at a configurable max-retry-hours window (default
  reasonable, 24h cap) and migrates payloads to a dead-letter
  table on timeout. With a finite timeout "forever" stops being
  accurate. Grammar acceptance unchanged in v0.9.x; full
  implementation lands v0.10.

### Fixed

- `datetime.utcnow()` (deprecated in Python 3.12, removed in 3.13)
  → `datetime.now(timezone.utc)` migration in `expression/cel.py`
  and `validation/dependents.py`. Wire format preserved
  byte-for-byte via `.replace("+00:00", "Z")` so audit columns,
  CEL `now` bindings, and any external timestamp consumers see
  identical strings.

### Suite

268 tests passing on Windows (was 261; +7 for the new
`TestMakeAnonymousPrincipal` class in `test_provider_contracts.py`).

## [0.9.0] — 2026-04-30

The opening release of `termin-core`. Phase 7 of the v0.9 milestone
extracted the framework-free contract surface that any conforming
Termin runtime imports — Provider Protocols, IR types, expression
evaluation, confidentiality, errors, validation, state-machine
rules, routing types, and 6 CRUD handlers — into this sibling
package. `termin-server` is the reference framework adapter;
alternate runtimes (an alternate Termin runtime, third-party
language ports) can implement against `termin-core` directly
without depending on FastAPI / SQLite / Anthropic / Jinja2.

**Release-day suite:** 285 tests passing on Windows (261 unit/contract
+ 24 conformance pack additions), coverage 68% (from 36% pre-pack).

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
