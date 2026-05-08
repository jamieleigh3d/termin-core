# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Page composition utilities (v0.9.3 issue #4).

Framework-free helpers used by any conforming runtime that walks the
component tree of a page IR. The existing per-component
``PresentationProvider`` Protocol (in
``termin_core.providers.presentation_contract``) is **untouched** —
issue #4 was reframed during planning to "extract page composition
utilities to core, keep the existing Protocol."

What lives here:
  - ``extract_page_reqs(page)`` — walks the component tree and
    returns the data dependencies (sources, form target, reference
    lists, unique-validation fields, after-save hint).

What does NOT live here (and stays in ``termin-server``):
  - ``build_base_template`` / ``build_page_template`` /
    ``build_merged_page_template`` / ``build_nav_html`` — these
    return Jinja2 ``Template`` objects and use the Jinja-bound
    ``render_component`` dispatch table in
    ``termin_server/presentation.py``. They are not useful to an alt
    runtime that ships its own templating engine. A future templating
    abstraction could extract a string-producing variant; left for
    when an actual alt runtime needs it.
"""

from __future__ import annotations

from typing import Any, Mapping


def extract_page_reqs(page: Mapping[str, Any]) -> dict:
    """Walk a page IR's component tree and collect data dependencies.

    Returns a dict with the following keys:
      - ``sources`` — set of content names referenced by data_table /
        chat / aggregation / stat_breakdown components.
      - ``form_target`` — content name the page's form (if any) writes to.
      - ``ref_lists`` — set of content names referenced by
        ``field_input`` components with a ``reference_content``.
      - ``create_as`` — explicit ``create_as`` override on the form.
      - ``unique_fields`` — set of field names with
        ``validate_unique: true``.
      - ``after_save`` — explicit after-save behavior on the form.

    Pure function over the component tree; no framework or storage
    deps. Safe for any conforming runtime to call to determine what
    data it needs to fetch before rendering.
    """
    reqs = {
        "sources": set(), "form_target": None, "ref_lists": set(),
        "create_as": None, "unique_fields": set(), "after_save": None,
    }

    def _walk(children):
        for child in (children or []):
            t = child.get("type", "")
            p = child.get("props", {})
            if t in ("data_table", "chat"):
                src = p.get("source")
                if src:
                    reqs["sources"].add(src)
                _walk(child.get("children", []))
            elif t == "form":
                reqs["form_target"] = p.get("target")
                reqs["create_as"] = p.get("create_as")
                reqs["after_save"] = p.get("after_save")
                _walk(child.get("children", []))
            elif t == "field_input":
                ref = p.get("reference_content")
                if ref:
                    reqs["ref_lists"].add(ref)
                if p.get("validate_unique"):
                    reqs["unique_fields"].add(p.get("field", ""))
            elif t in ("aggregation", "stat_breakdown"):
                src = p.get("source")
                if src:
                    reqs["sources"].add(src)
            elif t == "section":
                _walk(child.get("children", []))

    _walk(page.get("children", []))
    return reqs


__all__ = ["extract_page_reqs"]
