# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""IR → JSON serialization helpers shared by the CLI's pkg builder
and any test/utility that needs the same canonical IR JSON shape.

Previously these lived in `cli.py` and were duplicated in
`backends/runtime.py`. Phase 2.x cleanup (post-(g)) moved them
here once the legacy `app.py` codegen path was retired.
"""

from dataclasses import asdict
from enum import Enum
import json


def ir_json_default(obj):
    """JSON encoder hook for IR dataclasses.

    Enums serialize as their `.name`; frozensets and sets sort and
    serialize as JSON arrays. Anything else raises TypeError per
    the json contract.
    """
    if isinstance(obj, Enum):
        return obj.name
    if isinstance(obj, (frozenset, set)):
        return sorted(
            (o.name if isinstance(o, Enum) else o) for o in obj)
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable")


def simplify_props(obj):
    """In-place: collapse `{"value": x, "is_expr": false}` PropValue
    dicts in the IR to the bare value `x`. Keeps the IR JSON shape
    closer to what conformance tests and human readers expect.
    """
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, dict) and set(v.keys()) == {"value", "is_expr"}:
                if not v["is_expr"]:
                    obj[k] = v["value"]
            elif isinstance(v, (dict, list)):
                simplify_props(v)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, dict) and set(item.keys()) == {"value", "is_expr"}:
                if not item["is_expr"]:
                    obj[i] = item["value"]
            elif isinstance(item, (dict, list)):
                simplify_props(item)


def serialize_ir(spec) -> str:
    """Convert an AppSpec dataclass to canonical indented JSON.

    Returns the same JSON shape that `.termin.pkg` archives embed
    and that `termin serve` reads at startup.
    """
    ir_dict = asdict(spec)
    simplify_props(ir_dict)
    return json.dumps(ir_dict, indent=2, default=ir_json_default)
