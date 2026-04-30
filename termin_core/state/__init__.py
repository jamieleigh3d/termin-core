# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""State-machine evaluation surface.

Pure rules over the IR's StateMachineSpec shape. The
``do_state_transition`` coroutine takes a StorageProvider and a
state-machines map and applies one transition atomically via
``storage.update_if``, raising
:class:`termin_core.errors.TerminConflictError` /
:class:`TerminScopeError` /
:class:`TerminNotFoundError` /
:class:`TerminBadRequestError` on failure modes the spec describes.

No framework dependency. HTTP adapters translate the runtime
exceptions to the appropriate status codes (400 / 403 / 404 / 409).
"""

from .machine import do_state_transition  # noqa: F401

__all__ = ["do_state_transition"]
