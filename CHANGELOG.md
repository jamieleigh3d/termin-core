# Changelog

## Unreleased — v0.9 in progress (feature/v0.9)

### Slice 7.1 — initial extraction (2026-04-30)

The `termin-core` repo opens. This first commit ships the skeleton:
package metadata, build configuration, framework-free dependency
declaration, and a smoke-test suite that includes a guard
(`test_no_fastapi_dependency`) ensuring `termin-core` cannot
silently regrow a transitive dependency on FastAPI / uvicorn /
aiosqlite / Anthropic / Jinja2.

The actual contract surface lands in subsequent commits as each
target subtree migrates from `termin-compiler/termin_runtime/` and
`termin-compiler/termin/ir.py` into this package. See the design
doc at
[`termin-compiler/docs/phase-7-termin-core-extraction-design.md`](https://github.com/jamieleigh3d/termin-compiler/blob/feature/v0.9/docs/phase-7-termin-core-extraction-design.md)
for the full Phase 7 plan.
