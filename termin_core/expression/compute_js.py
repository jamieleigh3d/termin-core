# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Client-side Compute JS registration builder.

Walks the IR's `computes` array and produces a JavaScript fragment that
registers each (eligible) Compute as a `ctx[name]` callable on the
browser side. This fragment is injected into SSR pages so client-side
highlight CEL expressions and inline form-default expressions can call
into the same Compute names that fire on the server.

This is the v0.9.3 extraction of `build_compute_js` from
`termin_server/pages.py`. The function is pure-string assembly with no
framework dependencies; any conforming runtime that serves SSR pages
can call it to produce identical client-side payloads.

Eligibility today: only single-line, single-output, single-input
Computes lower into a JS arrow function. Multi-line bodies and
service-identity computes stay server-side and are not emitted here.
"""

from __future__ import annotations

import re


def build_compute_js(ir: dict) -> str:
    """Build client-side compute JS registrations from the IR.

    Args:
        ir: the compiled AppSpec dict (as loaded from a `.termin.pkg`
            or freshly compiled by `termin compile`). Only the
            `computes` array is read.

    Returns:
        A `\\n`-joined JavaScript fragment of the form::

            ctx["compute_name"] = function(param) { return <expr>; };
            ctx["other_name"] = function(other) { return <expr>; };

        Empty string if the IR has no eligible computes.
    """
    parts = []
    for comp in ir.get("computes", []):
        body_lines = comp.get("body_lines", [])
        input_params = comp.get("input_params", [])
        if body_lines and input_params:
            param_name = input_params[0].get("name", "x") if input_params else "x"
            for line in body_lines:
                clean = line.strip().lstrip("[").rstrip("]").strip()
                m = re.match(r'(\w+)\s*=\s*(.*)', clean)
                if m:
                    expr = m.group(2).strip()
                    fname = comp["name"]["display"]
                    parts.append(
                        f'ctx["{fname}"] = function({param_name}) {{ return {expr}; }};')
                    break
    return "\n".join(parts)


__all__ = ["build_compute_js"]
