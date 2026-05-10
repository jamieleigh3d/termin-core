# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for termin_core.presentation.dispatch.

Per-component contract dispatch (v0.9.4 Path C): when an IR node
carries `node["contract"]` (lowered from `Using "<ns>.<contract>"`
in source), look up the bound provider and route there. SSR-capable
providers get their `render_ssr` called and the result inlined;
CSR-only providers get a mount-point div the JS hydrator picks up.

This module lives in termin-core (not termin-server) so any
conforming runtime — alt runtimes building on
``termin-core>=0.9.4`` — inherit the dispatch + the mount-point
HTML attribute conventions without reimplementing them. termin-server's
``presentation.py::render_component`` calls into these helpers as
its first dispatch branch and falls back to its server-specific
type-based renderer table only when no contract is bound."""

from __future__ import annotations

import html
import json

import pytest


# ── Fakes mirroring real provider shapes ──

class _CsrOnlyProvider:
    declared_contracts = ("airlock.cosmic-orb", "airlock.scenario-narrative")
    render_modes = ("csr",)

    def render_ssr(self, contract, ir_fragment, data, principal_context):
        raise NotImplementedError("CSR-only provider")


class _SsrCapableProvider:
    declared_contracts = ("custom.greeting",)
    render_modes = ("ssr", "csr")

    def render_ssr(self, contract, ir_fragment, data, principal_context):
        name = (ir_fragment or {}).get("props", {}).get("name", "world")
        return f'<span data-custom-greeting="{contract}">Hello, {name}!</span>'


class _RaisingProvider:
    declared_contracts = ("custom.boom",)
    render_modes = ("ssr",)

    def render_ssr(self, contract, ir_fragment, data, principal_context):
        raise RuntimeError("provider exploded")


class _NoModesProvider:
    declared_contracts = ("custom.silent",)
    render_modes = ()

    def render_ssr(self, *args, **kwargs):
        raise NotImplementedError


# ── Mount-point HTML attribute constants ──

class TestMountPointConstants:
    """The HTML attribute names the SSR pipeline emits and the JS
    hydrator picks up are a runtime contract — every conforming
    runtime must use the same names so termin.js's hydrator works
    against any runtime's emitted markup.

    Surfacing them as named constants from core (rather than
    string literals scattered through provider code) prevents
    drift."""

    def test_mount_attr_name(self):
        from termin_core.presentation.dispatch import CSR_MOUNT_ATTR
        assert CSR_MOUNT_ATTR == "data-termin-csr-mount"

    def test_contract_attr_name(self):
        from termin_core.presentation.dispatch import CSR_CONTRACT_ATTR
        assert CSR_CONTRACT_ATTR == "data-termin-contract"

    def test_ir_attr_name(self):
        from termin_core.presentation.dispatch import CSR_IR_ATTR
        assert CSR_IR_ATTR == "data-termin-ir"

    def test_hydrated_attr_name(self):
        from termin_core.presentation.dispatch import CSR_HYDRATED_ATTR
        assert CSR_HYDRATED_ATTR == "data-termin-hydrated"


# ── find_provider_for_contract ──

class TestFindProviderForContract:
    def test_returns_provider_on_exact_contract_match(self):
        from termin_core.presentation.dispatch import find_provider_for_contract
        provider = _CsrOnlyProvider()
        providers = [
            ("presentation-base.text", "tailwind", object()),
            ("airlock.cosmic-orb", "airlock", provider),
        ]
        assert find_provider_for_contract(
            providers, "airlock.cosmic-orb") is provider

    def test_returns_none_when_not_bound(self):
        from termin_core.presentation.dispatch import find_provider_for_contract
        providers = [("presentation-base.text", "tailwind", object())]
        assert find_provider_for_contract(
            providers, "airlock.cosmic-orb") is None

    def test_returns_none_for_empty_providers_list(self):
        from termin_core.presentation.dispatch import find_provider_for_contract
        assert find_provider_for_contract([], "airlock.cosmic-orb") is None

    def test_returns_none_for_none_providers(self):
        from termin_core.presentation.dispatch import find_provider_for_contract
        assert find_provider_for_contract(None, "airlock.cosmic-orb") is None

    def test_returns_none_for_empty_contract_string(self):
        from termin_core.presentation.dispatch import find_provider_for_contract
        providers = [("airlock.cosmic-orb", "airlock", _CsrOnlyProvider())]
        assert find_provider_for_contract(providers, "") is None
        assert find_provider_for_contract(providers, None) is None

    def test_no_partial_or_prefix_matches(self):
        """`airlock.cosmic-orb` MUST NOT match
        `airlock.cosmic-orb-v2` — exact match only."""
        from termin_core.presentation.dispatch import find_provider_for_contract
        providers = [
            ("airlock.cosmic-orb-v2", "airlock", _CsrOnlyProvider()),
        ]
        assert find_provider_for_contract(
            providers, "airlock.cosmic-orb") is None


# ── render_via_provider ──

class TestRenderViaProviderSsr:
    def test_ssr_capable_inlines_provider_output(self):
        from termin_core.presentation.dispatch import render_via_provider
        provider = _SsrCapableProvider()
        node = {"type": "text", "contract": "custom.greeting",
                "props": {"name": "JL"}}
        out = render_via_provider(node, "custom.greeting", provider)
        assert "Hello, JL!" in out
        assert 'data-custom-greeting="custom.greeting"' in out
        assert "data-termin-csr-mount" not in out

    def test_provider_exception_renders_visible_error(self):
        from termin_core.presentation.dispatch import render_via_provider
        provider = _RaisingProvider()
        node = {"type": "text", "contract": "custom.boom"}
        out = render_via_provider(node, "custom.boom", provider)
        # Visible error markup so the failure is obvious in browser.
        assert "RuntimeError" in out
        assert "provider exploded" in out
        assert "custom.boom" in out
        assert "data-termin-provider-error" in out

    def test_provider_not_implemented_falls_back_to_csr_mount(self):
        """A provider that declared SSR support but raised
        NotImplementedError gets a CSR mount-point as a safety net
        IF it also declares CSR support. Otherwise the
        empty-modes branch fires."""
        from termin_core.presentation.dispatch import render_via_provider

        class _BothModesButNotImplemented:
            declared_contracts = ("custom.lazy",)
            render_modes = ("ssr", "csr")

            def render_ssr(self, *args, **kwargs):
                raise NotImplementedError

        provider = _BothModesButNotImplemented()
        node = {"type": "text", "contract": "custom.lazy"}
        out = render_via_provider(node, "custom.lazy", provider)
        assert "data-termin-csr-mount" in out
        assert 'data-termin-contract="custom.lazy"' in out


class TestRenderViaProviderCsr:
    def test_csr_only_emits_mount_point(self):
        from termin_core.presentation.dispatch import render_via_provider
        provider = _CsrOnlyProvider()
        node = {"type": "data_table", "contract": "airlock.cosmic-orb",
                "props": {"source": "scenes"}}
        out = render_via_provider(node, "airlock.cosmic-orb", provider)
        assert "data-termin-csr-mount" in out
        assert 'data-termin-contract="airlock.cosmic-orb"' in out
        assert "data-termin-ir" in out
        # No legacy type-based markup.
        assert "<table" not in out

    def test_ir_round_trips_through_html_escape(self):
        """The IR JSON gets HTML-attribute-escaped, then JS parses
        it back. Special chars (quotes, ampersands, less-than)
        must round-trip cleanly."""
        from termin_core.presentation.dispatch import render_via_provider
        provider = _CsrOnlyProvider()
        node = {
            "type": "data_table",
            "contract": "airlock.scenario-narrative",
            "props": {
                "lines": [
                    {"text": 'A "quoted" line', "kind": "narrative"},
                    {"text": "Tom & Jerry < 1940", "kind": "alert"},
                ],
            },
        }
        out = render_via_provider(
            node, "airlock.scenario-narrative", provider)
        import re
        match = re.search(r'data-termin-ir="([^"]*)"', out)
        assert match
        decoded = html.unescape(match.group(1))
        round_tripped = json.loads(decoded)
        assert round_tripped["props"]["lines"][0]["text"] == 'A "quoted" line'
        assert round_tripped["props"]["lines"][1]["text"] == "Tom & Jerry < 1940"

    def test_contract_name_escaped_in_attr(self):
        """The contract name itself never contains HTML metacharacters
        in normal use, but the function must escape it defensively
        — a typo in `Using` could put one in."""
        from termin_core.presentation.dispatch import render_via_provider
        provider = _CsrOnlyProvider()
        node = {"type": "x", "contract": 'evil"name'}
        out = render_via_provider(node, 'evil"name', provider)
        # No raw quote-injection.
        assert 'data-termin-contract="evil&quot;name"' in out


class TestRenderViaProviderEdgeCases:
    def test_no_render_modes_emits_diagnostic(self):
        from termin_core.presentation.dispatch import render_via_provider
        provider = _NoModesProvider()
        node = {"type": "text", "contract": "custom.silent"}
        out = render_via_provider(node, "custom.silent", provider)
        # Visible diagnostic, not silent failure.
        assert "no render_modes" in out or "empty-modes" in out
        assert "custom.silent" in out

    def test_missing_render_modes_attribute_treated_as_empty(self):
        """A provider object that didn't declare render_modes at all
        gets the same treatment as one with an empty tuple."""
        from termin_core.presentation.dispatch import render_via_provider

        class _MalformedProvider:
            declared_contracts = ("custom.malformed",)
            # render_modes intentionally absent

            def render_ssr(self, *args, **kwargs):
                raise NotImplementedError

        provider = _MalformedProvider()
        node = {"type": "text", "contract": "custom.malformed"}
        out = render_via_provider(node, "custom.malformed", provider)
        assert "custom.malformed" in out  # diagnostic mentions it
