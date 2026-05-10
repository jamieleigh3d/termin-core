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
- ``dispatch`` (v0.9.4 Path C) — per-component contract dispatch.
  ``find_provider_for_contract`` looks up the provider bound to a
  qualified contract name; ``render_via_provider`` calls
  ``render_ssr`` for SSR-capable providers or emits a CSR mount-
  point ``<div>`` for CSR-only providers. The four ``CSR_*_ATTR``
  constants name the HTML attributes the JS hydrator picks up.
- ``provider_bindings`` (v0.9.4 Path C) —
  ``build_presentation_provider_bindings`` resolves a deploy_config's
  ``bindings.presentation`` map into the
  ``(contract, product, instance)`` triples a runtime keeps in
  ``ctx.presentation_providers``. Includes the namespace-expansion
  fallback that asks a provider's ``declared_contracts`` when no
  ``presentation-base`` or contract-package YAML claims the
  namespace.

Server-side template engines (Jinja2 in termin-server,
``build_page_template`` / ``build_merged_page_template``) stay in
the per-runtime layer. They call into ``dispatch.render_via_provider``
as their first dispatch branch and fall back to their own type-based
renderer table only when no contract is bound.
"""
