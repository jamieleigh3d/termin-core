# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for termin_core.presentation.provider_bindings.

The presentation-binding resolver: walks the deploy_config's
``bindings.presentation`` and ``presentation.bindings`` sections,
expands each binding (per-contract or per-namespace) into the
list of (qualified_contract, product_name, instance) triples the
runtime keeps in ``ctx.presentation_providers``.

Lives in core (v0.9.4 Path C refactor) so alt runtimes inherit the
expansion logic — the v0.9.3 narrative was *"alt runtime can build
on termin-core>=0.9.3 alone"* and provider-binding resolution was
the last presentation-related piece still in termin-server.

Three namespace-expansion paths:
  1. ``presentation-base`` namespace → the hardcoded
     PRESENTATION_BASE_CONTRACTS list.
  2. Any namespace declared by a loaded contract package
     (``contract_package_registry`` is non-None and contains the
     namespace) → the package's declared contracts.
  3. Any other namespace → instantiate the provider, read its
     ``declared_contracts``, expand to those that start with
     ``<namespace>.``. Makes a per-provider package (the Airlock
     shape) deployable with one binding line.
"""

from __future__ import annotations

import pytest

from termin_core.providers.contracts import (
    Category, ContractDefinition, ContractRegistry, Tier,
)
from termin_core.providers.registry import ProviderRegistry


# ── Fakes ──

class _CsrOnlyProvider:
    def __init__(self, declared=("airlock.cosmic-orb",
                                 "airlock.scenario-narrative")):
        self.declared_contracts = declared
        self.render_modes = ("csr",)

    def render_ssr(self, *args, **kwargs):
        raise NotImplementedError


class _MultiNamespaceProvider:
    """One provider declaring contracts in two different namespaces."""

    def __init__(self):
        self.declared_contracts = (
            "airlock.cosmic-orb",
            "other-ns.something-else",
        )
        self.render_modes = ("csr",)

    def render_ssr(self, *args, **kwargs):
        raise NotImplementedError


def _registries_with_provider(product_name: str, factory):
    """Set up minimal registries and register a provider against
    one airlock contract — the populator's `_get_or_create` only
    needs *some* registered record matching the product name."""
    contract_registry = ContractRegistry()
    contract_registry.register_contract(ContractDefinition(
        name="airlock.cosmic-orb",
        category=Category.PRESENTATION,
        tier=Tier.TIER_2,
        naming="named",
        description="test",
    ))
    provider_registry = ProviderRegistry()
    provider_registry.register(
        category=Category.PRESENTATION,
        contract_name="airlock.cosmic-orb",
        product_name=product_name,
        factory=factory,
        contract_registry=contract_registry,
    )
    return provider_registry, contract_registry


# ── build_presentation_provider_bindings ──

class TestBuildPresentationProviderBindings:
    def _build(self, deploy_config, provider_registry,
               contract_package_registry=None):
        from termin_core.presentation.provider_bindings import (
            build_presentation_provider_bindings,
        )
        return build_presentation_provider_bindings(
            deploy_config=deploy_config,
            provider_registry=provider_registry,
            contract_package_registry=contract_package_registry,
        )

    def test_per_contract_binding_yields_one_triple(self):
        prov_reg, _ = _registries_with_provider(
            "airlock-fake", lambda cfg: _CsrOnlyProvider())
        deploy = {"bindings": {"presentation": {
            "airlock.cosmic-orb": {"provider": "airlock-fake", "config": {}},
        }}}
        result = self._build(deploy, prov_reg)
        contracts = {c for c, _p, _i in result}
        assert contracts == {"airlock.cosmic-orb"}

    def test_namespace_binding_expands_via_declared_contracts(self):
        """The Path C fallback. Without a contract-package YAML or
        a hardcoded list, a namespace binding must still expand by
        asking the provider what it declares."""
        prov_reg, _ = _registries_with_provider(
            "airlock-fake", lambda cfg: _CsrOnlyProvider())
        deploy = {"bindings": {"presentation": {
            "airlock": {"provider": "airlock-fake", "config": {}},
        }}}
        result = self._build(deploy, prov_reg)
        contracts = {c for c, _p, _i in result}
        assert "airlock.cosmic-orb" in contracts
        assert "airlock.scenario-narrative" in contracts

    def test_namespace_expansion_does_not_leak_across_namespaces(self):
        """A provider declaring contracts in multiple namespaces only
        gets bound under the namespace the deploy config names."""
        prov_reg, _ = _registries_with_provider(
            "multi-fake", lambda cfg: _MultiNamespaceProvider())
        deploy = {"bindings": {"presentation": {
            "airlock": {"provider": "multi-fake", "config": {}},
        }}}
        result = self._build(deploy, prov_reg)
        contracts = {c for c, _p, _i in result}
        assert "airlock.cosmic-orb" in contracts
        assert "other-ns.something-else" not in contracts

    def test_presentation_base_namespace_uses_hardcoded_list(self):
        """`presentation-base` expansion is special — uses the
        PRESENTATION_BASE_CONTRACTS list, not the provider's
        declared_contracts. This preserves the v0.9.3 behavior
        for backward-compat."""
        from termin_core.providers.presentation_contract import (
            PRESENTATION_BASE_CONTRACTS,
        )

        class _FakeBaseProvider:
            declared_contracts = ()  # intentionally empty
            render_modes = ("ssr", "csr")

            def render_ssr(self, *args, **kwargs):
                return ""

            def csr_bundle_url(self):
                return None

        prov_reg, contract_reg = _registries_with_provider(
            "tailwind-fake", lambda cfg: _FakeBaseProvider())
        deploy = {"bindings": {"presentation": {
            "presentation-base": {"provider": "tailwind-fake", "config": {}},
        }}}
        result = self._build(deploy, prov_reg)
        contracts = {c for c, _p, _i in result}
        for short in PRESENTATION_BASE_CONTRACTS:
            assert f"presentation-base.{short}" in contracts

    def test_no_presentation_base_binding_synthesizes_default(self):
        """Pre-existing convenience: when no presentation-base
        binding is declared, synthesize one to ``tailwind-default``.
        Only fires if a tailwind-default factory is registered;
        otherwise no-op."""
        # Register tailwind-default so synthesis succeeds.
        contract_registry = ContractRegistry()
        contract_registry.register_contract(ContractDefinition(
            name="presentation-base.text",
            category=Category.PRESENTATION,
            tier=Tier.TIER_1,
            naming="named",
            description="test",
        ))
        prov_reg = ProviderRegistry()

        class _FakeTailwind:
            declared_contracts = ("presentation-base.text",)
            render_modes = ("ssr", "csr")

            def render_ssr(self, *args, **kwargs):
                return ""

            def csr_bundle_url(self):
                return None

        prov_reg.register(
            category=Category.PRESENTATION,
            contract_name="presentation-base.text",
            product_name="tailwind-default",
            factory=lambda cfg: _FakeTailwind(),
            contract_registry=contract_registry,
        )
        # No presentation-base binding declared — should still get one.
        deploy = {"bindings": {"presentation": {}}}
        result = self._build(deploy, prov_reg)
        contracts = {c for c, _p, _i in result}
        assert "presentation-base.text" in contracts

    def test_unknown_product_silently_skipped(self):
        """A binding whose product isn't registered gets skipped
        — populator stays advisory; deploy-time validation is the
        right place to fail-closed (BRD #2 §8.5
        required_contracts)."""
        prov_reg = ProviderRegistry()
        deploy = {"bindings": {"presentation": {
            "airlock.cosmic-orb": {"provider": "not-registered"},
        }}}
        result = self._build(deploy, prov_reg)
        assert result == []

    def test_empty_deploy_config_returns_empty(self):
        prov_reg = ProviderRegistry()
        assert self._build({}, prov_reg) == []
        assert self._build({"bindings": {}}, prov_reg) == []
        assert self._build({"bindings": {"presentation": {}}}, prov_reg) == []

    def test_provider_instance_cached_across_contracts(self):
        """A provider that gets bound to multiple contracts (because
        its declared_contracts list has several) should be the SAME
        instance for each — instantiating once per contract would
        be wasteful and break stateful providers."""
        prov_reg, _ = _registries_with_provider(
            "airlock-fake", lambda cfg: _CsrOnlyProvider())
        deploy = {"bindings": {"presentation": {
            "airlock": {"provider": "airlock-fake", "config": {}},
        }}}
        result = self._build(deploy, prov_reg)
        instances = {id(i) for _c, _p, i in result}
        assert len(instances) == 1, (
            "expected one instance shared across all bound contracts")

    def test_nested_presentation_bindings_shape_also_works(self):
        """BRD #2 §11.2 documents two locations for presentation
        bindings: ``bindings.presentation.X`` (flat) and
        ``presentation.bindings.X`` (nested). Both must be
        recognized."""
        prov_reg, _ = _registries_with_provider(
            "airlock-fake", lambda cfg: _CsrOnlyProvider())
        deploy = {"presentation": {"bindings": {
            "airlock.cosmic-orb": {"provider": "airlock-fake", "config": {}},
        }}}
        result = self._build(deploy, prov_reg)
        contracts = {c for c, _p, _i in result}
        assert "airlock.cosmic-orb" in contracts
