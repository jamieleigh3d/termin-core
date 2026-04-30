# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Termin core library.

Contract Protocols, IR types, expression evaluation, and routing
dispatch surface that any conforming Termin runtime imports from.
Framework-free — no dependency on FastAPI, uvicorn, aiosqlite, or
any other concrete hosting layer. Reference runtime code lives in
the sibling ``termin-server`` package; the compiler lives in
``termin-compiler``.
"""

__version__ = "0.9.0"
