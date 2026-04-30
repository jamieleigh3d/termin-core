# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""TerminError model and TerminAtor error router for the Termin runtime."""

from datetime import datetime


class TerminError:
    def __init__(self, source, kind, message, context=None, boundary_path=None):
        self.source = source
        self.kind = kind  # validation, authorization, state, timeout, schema, internal, external
        self.message = message
        self.timestamp = datetime.now().isoformat()
        self.context = context or ""
        self.boundary_path = boundary_path or []


class TerminAtor:
    def __init__(self, expr_eval=None):
        self._handlers = {}
        self._typed_handlers = []  # list of {source, condition, actions, is_catch_all}
        self._error_log = []
        self._expr_eval = expr_eval

    def set_expr_eval(self, expr_eval):
        self._expr_eval = expr_eval

    def route(self, error: TerminError):
        self._error_log.append(error.__dict__)
        # Try typed error handlers first
        handled = self.handle_error(error)
        if handled:
            return
        # Try boundary-specific handler
        for boundary in error.boundary_path:
            if boundary in self._handlers:
                self._handlers[boundary](error)
                return
        # Global fallback
        print(f"[TerminAtor] {error.kind}: {error.message} (from {error.source})")

    def register_handler(self, source_or_boundary, handler_or_spec=None):
        if isinstance(handler_or_spec, dict):
            self._typed_handlers.append(handler_or_spec)
        elif callable(handler_or_spec):
            self._handlers[source_or_boundary] = handler_or_spec
        else:
            self._handlers[source_or_boundary] = handler_or_spec

    def handle_error(self, error: TerminError):
        """Evaluate typed handlers in declaration order."""
        for handler in self._typed_handlers:
            # Match source
            if handler.get("is_catch_all"):
                pass  # catch-all matches everything
            elif handler.get("source") and handler["source"] != error.source:
                continue
            # Evaluate condition if present
            condition = handler.get("condition")
            if condition:
                try:
                    ctx = {"error": error.__dict__}
                    if self._expr_eval:
                        result = self._expr_eval.evaluate(condition, ctx)
                    else:
                        result = False
                    if not result:
                        continue
                except Exception:
                    continue
            # Execute actions in sequence
            for action in handler.get("actions", []):
                kind = action.get("kind")
                if kind == "retry":
                    count = action.get("retry_count", 0)
                    print(f"[TerminAtor] Retry {count} times for {error.source}")
                elif kind == "disable":
                    target = action.get("target", "")
                    print(f"[TerminAtor] Disabling {target} for {error.source}")
                elif kind == "escalate":
                    print(f"[TerminAtor] Escalating error from {error.source}")
                    return False  # Let it propagate
                elif kind == "create":
                    target = action.get("target", "")
                    print(f"[TerminAtor] Creating {target} event for {error.source}")
                elif kind == "notify":
                    target = action.get("target", "")
                    print(f"[TerminAtor] Notifying {target} about {error.source}")
                elif kind == "set":
                    expr = action.get("expr", "")
                    print(f"[TerminAtor] Setting {expr} for {error.source}")
                log_level = action.get("log_level")
                if log_level:
                    print(f"[TerminAtor] [{log_level}] {error.message}")
            return True
        return False

    def get_error_log(self):
        return self._error_log

    def get_typed_handlers(self):
        return self._typed_handlers
