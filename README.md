# termin-core

The framework-free contract surface for any conforming Termin runtime.

## What this is

`termin-core` carries the Protocols, IR types, expression evaluator,
predicate AST, deploy-config parser, and routing-dispatch substrate
that every Termin runtime depends on. It has **no** dependency on
FastAPI, uvicorn, aiosqlite, Anthropic, Jinja2, or any other concrete
hosting/storage/compute implementation. Anything that needs a network
socket, a file handle, or a process boundary lives outside this
package.

The reference HTTP runtime — FastAPI app factory, SQLite storage
provider, Anthropic LLM/agent providers, Tailwind SSR renderer — lives
in the sibling [`termin-server`](https://github.com/jamieleigh3d/termin-server)
package. The compiler that turns `.termin` source into `.termin.pkg`
artifacts lives in
[`termin-compiler`](https://github.com/jamieleigh3d/termin-compiler).
The conformance test suite lives in
[`termin-conformance`](https://github.com/jamieleigh3d/termin-conformance).

## Who imports this

- The reference runtime (`termin-server`) — full surface.
- The compiler (`termin-compiler`) — IR types only; uses them as the
  output type of its `lower()` pass.
- An alternate Termin runtime — full surface, plus its own framework
  adapter to bind the routing dispatch onto its preferred HTTP /
  WebSocket implementation.
- The conformance suite — Provider Protocol shapes for the contract
  conformance tests.

## What's inside

| Subpackage | What it carries |
|---|---|
| `termin_core.ir` | IR dataclasses (AppSpec, ContentSchema, FieldSpec, PageEntry, ComponentNode, …) and canonical JSON serialization |
| `termin_core.providers` | Contract Protocols (Identity, Storage, Compute, Channels, Presentation), `Category`, `Tier`, `ContractDefinition`, `ContractRegistry`, binding resolution, deploy-config parser |
| `termin_core.expression` | CEL expression evaluator + Predicate AST |
| `termin_core.confidentiality` | `Redacted` sentinel, redaction rules |
| `termin_core.identity` | `Principal`, `PrincipalContext` value types |
| `termin_core.errors` | `TerminAtor` error router + envelope shapes |
| `termin_core.validation` | D-19 dependent-values + one-of-values validators |
| `termin_core.state` | Pure state-machine evaluator (no IO) |
| `termin_core.routing` | Framework-agnostic dispatch (REST + WebSocket) — *added in slice 7.2* |
| `termin_core.builtins` | Pure-Python provider builtins (CEL compute, stub identity) |

## Status

**v0.9.2 — released 2026-05-05.** Conversation-field IR additions
release. `ir_version` bumps **0.9.0 → 0.9.2** to match the additive
shape changes: two new base types (`structured`, `conversation`),
the `Verb.APPEND` CRUD verb + matching `RouteSpec`, optional
`ComputeSpec.conversation_source`, action-list `WhenRuleSpec`, and
the `ConversationContext` runtime context type. All additions are
backwards-compatible — v0.9.0 / v0.9.1 sources and runtimes
continue to work unchanged. 273 tests passing.

### v0.9 release arc

- **v0.9.0** (2026-04-30) — opening release. Phase 7 of the v0.9
  Termin milestone extracted this contract surface out of
  `termin-compiler/termin_runtime/` and
  `termin-compiler/termin/{ir,ir_serialize}.py` over slices 7.1
  through 7.5. The `termin_runtime/` shim layer that carried tests
  through the transition was deleted in slice 7.5a; slice 7.5b
  dropped the legacy `User.PascalCase` CEL surface and the
  `legacy_user_dict` carrier in favor of the v0.9 `the user`
  shape. 285 tests, coverage 68%.
- **v0.9.1** (2026-05-01) — correctness + hygiene patch. Added
  `make_anonymous_principal(session_marker)` (anonymous is now a
  typed Principal with id `anonymous:<sanitized>`), wired
  `trigger_compute_handler` to forward the resolved Principal as
  `invoked_by`, and renamed the `failure_mode` enum
  `queue-and-retry-forever` → `queue-and-retry`. IR shape
  unchanged; `ir_version` stayed at 0.9.0. 268 tests.
- **v0.9.2** (2026-05-05) — conversation-field IR additions; see
  above.

## License

Apache 2.0 — see [LICENSE](LICENSE).
