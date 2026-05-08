# Changelog

## [0.9.3] — 2026-05-07

The runtime extraction release. Internal API surface only — no IR
change (`ir_version` stays at 0.9.2). Per `RELEASE_PROCESS.md` §2,
this is a patch release: additive Python API, no removal of public
surface. The cross-repo tech design lives at
`termin-compiler/docs/termin-v0.9.3-runtime-extraction-tech-design.md`.

This release widens `termin-core` so an alternate Termin runtime
(AWS-native, third-party-Rust, anything else) can build on
`termin-core>=0.9.3` alone, without inheriting FastAPI, aiosqlite,
or Anthropic transitively from `termin-server`.

### Added — Runtime infrastructure (top-level modules)

- **`termin_core.events`** — `EventBus` for in-process pub/sub with
  channel-prefix subscription filtering.
- **`termin_core.scheduler`** — `Scheduler`,
  `parse_schedule_interval` for periodic Compute execution.
- **`termin_core.transaction`** — `Transaction`, `ContentSnapshot`,
  `StagedWrite` for snapshot-isolation Compute write staging.
- **`termin_core.reflection`** — `ReflectionEngine`,
  `register_reflection_with_expr_eval`. Conformance asserts on
  this output shape.

### Added — Security + accessibility primitives

- **`termin_core.boundaries`** — `build_boundary_maps`,
  `check_boundary_access`, `check_boundary_identity`. Pure functions
  over IR dicts.
- **`termin_core.colorblind`** — CVD simulation + WCAG contrast
  helpers (`simulate_cvd`, `contrast_ratio`, `relative_luminance`,
  `cvd_distinguishable`, `hex_to_rgb`).
- **`termin_core.presentation.markdown_sanitizer`** — the
  BRD-mandated `sanitize_markdown` for the
  `presentation-base.markdown` contract. Every conforming runtime
  serving that contract uses this implementation to keep the wire
  shape consistent.

### Added — IR migrations

- **`termin_core.migrations`** package with `classifier`,
  `validate`, `introspect`, `ack`, `errors` submodules. Pure
  framework-free migration logic; conformance imports from this
  namespace and asserts on its behavior.

### Added — Channel dispatch

- **`termin_core.channels`** — `ChannelDispatcher` connects declared
  Channels to external services with scope enforcement, type
  validation, and delivery semantics.
- **`termin_core.channel_config`** — deploy-config loader,
  validator, and channel config dataclasses.
- **`termin_core.channel_ws`** — outbound WebSocket connection
  with auto-reconnect (optional `websockets` library; graceful
  fallback when not installed).

### Added — Page composition + client-side compute JS

- **`termin_core.expression.compute_js.build_compute_js(ir)`** —
  client-side Compute JS registration builder for SSR pages.
- **`termin_core.presentation.compose.extract_page_reqs(page)`** —
  component-tree walker that returns the data dependencies
  (sources, form target, reference lists, unique-validation fields,
  after-save hint) the runtime must satisfy before rendering. The
  Jinja-bound `build_*template` functions stay in `termin-server`
  (per the no-Jinja-in-core rule); they're not useful to alt
  runtimes that ship their own templating engine.

### Added — HTTP routing surface

- **`termin_core.routing.append`** — v0.9.2's append CRUD verb
  handler: `append_to_field(ctx, *, content_ref, key_val,
  field_name, payload, user, row_filter)`,
  `AppendValidationError`, `AppendNotFoundError`,
  `CANONICAL_KINDS`. Uses `ctx.storage` (StorageProvider Protocol)
  for storage access.
- **`termin_core.routing.dispatch`** — `build_route_specs(ctx)`
  walks the IR's pre-computed routes plus `ir.channels` and
  returns `list[RouteSpec]`; `dispatch_http_request(ctx, request)`
  is a convenience function that path-matches and dispatches to
  the appropriate per-class handler. Adapters that prefer
  per-route binding (FastAPI, Starlette) iterate the spec list;
  adapters that prefer single-entry-point dispatch (raw ASGI) call
  the convenience.

### Added — Compute orchestration

- **`termin_core.compute`** package with `materialize` submodule.
  SDK-agnostic transformation helpers: `materialize_to_anthropic`
  (canonical conversation-entry → Anthropic-shape messages array,
  per v0.9.2 §11.4), `entry_role`, `build_content_blocks`,
  `build_invokable_compute_tools`, `build_output_tool` (basic
  scaffold), `build_agent_tools` (basic scaffold),
  `truncate_purpose`, `purpose_property`, `add_purpose_to_tool`,
  plus `CANONICAL_KINDS_USER_ROLE`,
  `CANONICAL_KINDS_ASSISTANT_ROLE`, `PURPOSE_MAX_WORDS`,
  `PURPOSE_TOOL_DESCRIPTION` constants and the
  `ConversationMaterializationError` exception.
- The provider Protocols themselves (`DefaultCelComputeProvider`,
  `LlmComputeProvider`, `AiAgentComputeProvider`) live in
  `termin_core.providers.compute_contract` (unchanged from
  v0.9.0).

### Test count

- 273 passing.

## [0.9.2] — 2026-05-05

The conversation-field IR additions release. Adds the IR types,
contract Protocol additions, and routing dispatch surface for the
v0.9.2 conversation-field work; the matching compiler/runtime
surface lands in `termin-compiler`, `termin-server`, and
`termin-conformance` v0.9.2.

`ir_version` bumps **0.9.0 → 0.9.2** because IR shape changed
additively (new base types, new verb, new routes, new compute
source). Per the v0.9.2 patch policy in `termin-compiler/RELEASE_PROCESS.md`
§2: additive IR fields are patches pre-v1.0.

### Added

- **`structured` and `conversation` base types** in
  `ir/types.py::FieldSpec` validation. Opaque-JSON and typed-
  message-log primitives that the runtime materializes per
  provider — termin-core stays framework-free; the actual JSON
  ↔ Anthropic translation lives in termin-server.
- **`Verb.APPEND`** in `ir/types.py::Verb` enum. Fifth CRUD verb
  alongside view/create/update/delete.
- **`RouteSpec` for `POST <resource>/{id}/<field>:append`** in
  `routing/dispatch.py`. New `RouteKind.APPEND` plus the
  matching dispatch shape; runtimes implement the handler in
  their hosting layer (the reference runtime in `termin-server`
  does this through `routes.py::_do_append`).
- **`Conversation` source on `ComputeSpec`** in `ir/types.py`.
  Optional `conversation_source: Optional[tuple[str, str]]`
  field — `(content_name, field_name)` — lowered from the
  `Conversation is X.Y` source line. Read by the runtime at
  agent-loop trigger time to know which conversation field to
  read history from and write replies back to.
- **When-rule action lists** in `ir/types.py::WhenRuleSpec`.
  `actions: tuple[ActionSpec, ...]` (replacing the single-action
  field; back-compat shim accepts the legacy single-action
  form). Each action carries its own `verb` and CEL-expression
  payload; runtimes dispatch each through the matching handler
  family.
- **`ActionSpec.append`** in `ir/types.py`. Append-action shape
  carrying `target` (content + field), `payload_expr`
  (CEL → entry envelope), and optional `parent_id_expr` for
  threaded entries.
- **`ConversationContext`** in `runtime_context.py`. Typed view
  over a conversation-field's entries. Methods:
  `materialize_for_agent()`, `append(kind, body, **opts)`,
  `entries_since(entry_id)`. Runtime providers receive this via
  `AgentContext.conversation` and use it to translate the
  conversation history to whatever message shape their backing
  agent expects.
- **`AgentContext.conversation`** field — `Optional[ConversationContext]`,
  populated by the runtime when the triggering compute declares
  `Conversation is X.Y`.

### Suite

273 tests passing (was 268; +5 from the new IR types, verb
membership, route shape, action-list lowering, and
ConversationContext API). Contract Protocol shape unchanged for
existing providers — the only Protocol additions are optional
fields/methods, so v0.9.0 / v0.9.1 providers continue to work
without modification.

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
