# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""IR (Intermediate Representation) types for Termin.

The IR (AppSpec and friends) sits between the compiler's analyzer pass
and any backend that consumes a compiled `.termin.pkg`. It is fully
resolved: all name resolution, cross-referencing, and inference happens
in the lowering pass. Backends read pre-resolved, immutable data.

All types are frozen dataclasses with tuples (not lists) for
immutability. The compiler builds them in `termin/lower.py`; this
package only carries the shapes.
"""

from .types import *  # noqa: F401, F403
from .serialize import (  # noqa: F401
    serialize_ir,
    ir_json_default,
    simplify_props,
)
