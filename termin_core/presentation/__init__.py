# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Presentation utilities for the Termin core library.

This package collects framework-free helpers used by any conforming
runtime that serves Termin's presentation surface:

- ``markdown_sanitizer`` — the BRD-mandated markdown sanitizer
  every runtime serving the ``presentation-base.markdown`` contract
  must use to keep the wire shape consistent.
- ``compose`` (v0.9.3) — page-level composition utilities that
  assemble a full HTML response from per-component renders. Pure
  functions over IR + rendered fragments; the existing per-component
  ``PresentationProvider`` Protocol (in
  ``termin_core.providers.presentation_contract``) is untouched.

Per-component rendering itself stays out of core: each provider
ships its own ``render_ssr`` / CSR bundle, and the server-side Jinja
machinery lives in ``termin_server.presentation``.
"""
