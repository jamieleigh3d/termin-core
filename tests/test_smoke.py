# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Smoke tests — termin-core is importable and reports its version."""


def test_termin_core_importable():
    import termin_core
    assert hasattr(termin_core, "__version__")


def test_termin_core_version_string():
    import termin_core
    assert termin_core.__version__ == "0.9.2"


def test_no_fastapi_dependency():
    """termin-core must not transitively depend on FastAPI. The whole
    point of the extraction is that alternate runtimes don't have to
    pull FastAPI to use the contract surface."""
    import importlib
    import termin_core  # noqa: F401 — load the package
    # Walk every termin_core submodule's already-imported transitive
    # closure (sys.modules at this point) and assert FastAPI didn't
    # arrive via any of them.
    import sys
    forbidden = {"fastapi", "uvicorn", "aiosqlite", "anthropic", "jinja2"}
    leaked = forbidden & set(sys.modules)
    assert not leaked, (
        f"termin-core leaked dependencies on framework packages: "
        f"{sorted(leaked)}. termin-core must stay framework-free; "
        f"these belong in termin-server.")
