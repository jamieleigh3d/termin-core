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

Pre-extraction work in progress. Slice 7.1 of Phase 7 — see
[`docs/phase-7-termin-core-extraction-design.md`](https://github.com/jamieleigh3d/termin-compiler/blob/feature/v0.9/docs/phase-7-termin-core-extraction-design.md)
in the compiler repo.

## License

Apache 2.0 — see [LICENSE](LICENSE).
