# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Per-component contract dispatch (v0.9.4 Path C).

Framework-free helpers for routing a component-IR node to its bound
presentation provider. Any conforming runtime calls these as the
first dispatch branch when walking the component tree; the runtime's
own type-based renderer table (or template engine) is the fallback.

The dispatch is a two-step contract:

  1. ``find_provider_for_contract(presentation_providers, contract)``
     looks up the provider bound to the qualified contract name.
     Returns the provider instance or ``None``.

  2. ``render_via_provider(node, contract, provider)`` either calls
     the provider's ``render_ssr`` (SSR-capable providers) and inlines
     the result, or emits a CSR mount-point ``<div>`` (CSR-only
     providers). The mount-point uses the data-attribute conventions
     defined as module-level constants below.

The mount-point HTML attribute names are part of the runtime
contract: termin.js's ``hydrateCsrMounts()`` (in termin-server's
bundled JS) walks for these exact names. Surfacing them as named
constants prevents drift between the producing renderer and the
consuming hydrator.

Lives in core (not termin-server) so an alt runtime building on
``termin-core>=0.9.4`` inherits the dispatch + the mount-point
contract without reimplementing them. v0.9.3's narrative was *"alt
runtime can build on termin-core>=0.9.3 alone for the framework-
free orchestration"*; per-component dispatch is the v0.9.4 piece
of that arc."""

from __future__ import annotations

import html as _html
import json as _json
from typing import Iterable, Optional


# ── Mount-point HTML attribute names ──
#
# These four attributes are the wire shape between the SSR pipeline
# and the JS hydrator. Any custom-namespace provider's renderer
# function (registered via Termin.registerRenderer) receives the
# mount element decorated with these attributes; any conforming
# runtime emitting CSR mount points must use the same names.

CSR_MOUNT_ATTR = "data-termin-csr-mount"
"""Boolean marker attribute. Presence means *this is a CSR mount
point* — the JS hydrator's selector matches on it."""

CSR_CONTRACT_ATTR = "data-termin-contract"
"""Carries the qualified contract name (e.g.
``airlock.cosmic-orb``). The hydrator looks up the registered
renderer for this contract name."""

CSR_IR_ATTR = "data-termin-ir"
"""Carries the IR fragment for the component, serialized as
HTML-escaped JSON. The hydrator parses it back into an object and
passes it to the renderer as the ``irFragment`` argument."""

CSR_HYDRATED_ATTR = "data-termin-hydrated"
"""Set to ``"true"`` by the hydrator after a successful mount.
Idempotency marker: the hydrator's selector excludes already-
hydrated elements so re-runs (e.g. after a late-arriving bundle)
don't double-mount on top of themselves."""


# ── find_provider_for_contract ──

def find_provider_for_contract(
    presentation_providers: Optional[Iterable[tuple]],
    contract: Optional[str],
):
    """Linear search for a provider bound to the exact contract name.

    Args:
      presentation_providers: an iterable of
        ``(qualified_contract_name, product_name, provider_instance)``
        triples — typically ``ctx.presentation_providers`` populated
        by ``build_presentation_provider_bindings``.
      contract: the fully-qualified contract name to look up
        (e.g. ``"airlock.cosmic-orb"``).

    Returns:
      The provider instance, or ``None`` if no exact match exists or
      the inputs are empty.

    The match is exact-string equality on the full contract name —
    no prefix or partial matching, so ``airlock.cosmic-orb`` does
    not spuriously match ``airlock.cosmic-orb-v2``."""
    if not presentation_providers or not contract:
        return None
    for entry in presentation_providers:
        try:
            c, _product, provider = entry
        except (TypeError, ValueError):
            # Defensive: malformed list entry. Skip rather than crash.
            continue
        if c == contract:
            return provider
    return None


# ── render_via_provider ──

def render_via_provider(node: dict, contract: str, provider) -> str:
    """Dispatch a node to its bound provider.

    SSR-capable provider (``"ssr"`` in ``provider.render_modes``):
      Call ``provider.render_ssr(contract, node, {}, {})`` and
      return the result inline. The two empty dicts are placeholders
      for ``PresentationData`` and ``PrincipalContext`` per the
      ``PresentationProvider`` Protocol — runtimes that bind data
      via a templating engine (the termin-server Jinja path) supply
      it through the template context, not the Protocol arguments,
      so the empty placeholders are correct for that case. A future
      v0.10 refinement may thread richer data through.

      If ``render_ssr`` raises ``NotImplementedError`` and the
      provider also declares ``"csr"``, fall through to the CSR
      mount-point path as a safety net. Any other exception is
      caught and rendered as visible markup so the failure is
      obvious in the browser rather than silently corrupting the
      page.

    CSR-only provider (only ``"csr"`` in ``render_modes``, or
    ``NotImplementedError`` fallback above):
      Emit a mount-point ``<div>`` using the four ``CSR_*_ATTR``
      constants from this module. The IR fragment is serialized
      to HTML-escaped JSON in ``CSR_IR_ATTR`` so the JS hydrator
      can parse it back.

    Provider with no declared render modes:
      Render a visible diagnostic so the misconfiguration shows
      up in the browser. Silent rendering of nothing would let
      the bug ship.
    """
    modes = tuple(getattr(provider, "render_modes", ()) or ())
    if "ssr" in modes:
        try:
            return provider.render_ssr(contract, node, {}, {})
        except NotImplementedError:
            # Declared SSR support but didn't actually implement it.
            # Fall through to the CSR mount-point path below if
            # CSR is also declared; otherwise the empty-modes
            # diagnostic at the bottom fires.
            pass
        except Exception as exc:
            return (
                f'<div class="text-red-600 text-sm" '
                f'data-termin-provider-error="{_html.escape(contract)}">'
                f'[provider {_html.escape(contract)} render_ssr failed: '
                f'{type(exc).__name__}: {_html.escape(str(exc))}]'
                f'</div>'
            )
    if "csr" in modes:
        ir_attr = _html.escape(_json.dumps(node), quote=True)
        return (
            f'<div {CSR_MOUNT_ATTR} '
            f'{CSR_CONTRACT_ATTR}="{_html.escape(contract)}" '
            f'{CSR_IR_ATTR}="{ir_attr}"></div>'
        )
    return (
        f'<div class="text-yellow-700 text-sm" '
        f'data-termin-provider-empty-modes="{_html.escape(contract)}">'
        f'[provider {_html.escape(contract)} declares no render_modes]'
        f'</div>'
    )


__all__ = [
    "CSR_MOUNT_ATTR",
    "CSR_CONTRACT_ATTR",
    "CSR_IR_ATTR",
    "CSR_HYDRATED_ATTR",
    "find_provider_for_contract",
    "render_via_provider",
]
