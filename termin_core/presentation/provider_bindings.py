# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Presentation provider binding resolver (v0.9.4 Path C).

Walks a deploy_config's ``bindings.presentation`` /
``presentation.bindings`` map and produces the
``(qualified_contract_name, product_name, provider_instance)``
triples a runtime keeps in ``ctx.presentation_providers``.

Three namespace-expansion paths:

  1. ``presentation-base`` namespace → expanded via the hardcoded
     ``PRESENTATION_BASE_CONTRACTS`` list. Preserves the v0.9.3
     behavior for the built-in contracts.

  2. Any namespace declared by a loaded contract package
     (``contract_package_registry`` is non-None and contains the
     namespace) → expanded via the package's declared contracts.
     Lets a contract-package YAML fan one binding out to every
     contract the package declares.

  3. Any other namespace (the v0.9.4 Path C addition) → instantiate
     the provider, read its ``declared_contracts``, expand to those
     that start with ``<namespace>.``. Makes a per-provider package
     (the Airlock-on-Termin shape) deployable with one binding line
     and no contract-package YAML.

Per-contract bindings (``bindings.presentation.<ns>.<contract>``,
key contains a dot) bypass namespace expansion and bind one
contract directly. They override any namespace binding for the
same contract.

Lives in core (not termin-server) per the v0.9.3 narrative — alt
runtimes building on ``termin-core>=0.9.4`` inherit the resolver
without reimplementing it. termin-server's
``_populate_presentation_providers`` is a thin wrapper that calls
this and assigns the result to ``ctx.presentation_providers``.
"""

from __future__ import annotations

from typing import Optional

from ..providers.contracts import Category
from ..providers.presentation_contract import PRESENTATION_BASE_CONTRACTS


def build_presentation_provider_bindings(
    deploy_config: dict,
    provider_registry,
    contract_package_registry: Optional[object] = None,
) -> list[tuple[str, str, object]]:
    """Resolve the deploy_config's presentation bindings into the
    triple list a runtime keeps in ``ctx.presentation_providers``.

    Args:
      deploy_config: the active deploy config dict. Bindings can
        live at ``deploy_config["bindings"]["presentation"]``
        (flat) or ``deploy_config["presentation"]["bindings"]``
        (nested) per BRD #2 §11.2; both shapes are recognized.
      provider_registry: the runtime's
        ``termin_core.providers.registry.ProviderRegistry``. Used
        to look up a factory by product name.
      contract_package_registry: optional — when present, namespace
        bindings whose key matches a loaded package's namespace
        expand via the package's declared contracts. The registry
        must expose ``namespaces()`` (iterable of namespace strings)
        and a private ``_packages`` dict keyed by namespace whose
        values have a ``.contracts`` iterable. v0.9 Phase 5c.1
        contract.

    Returns:
      A list of ``(qualified_contract_name, product_name,
      provider_instance)`` triples. One triple per bound contract.
      Provider instances are cached: a provider that gets bound
      across multiple contracts (because its ``declared_contracts``
      lists several, or the namespace expands to many contracts)
      shares one instance, not one per contract.

      Returns an empty list when the deploy_config has no
      presentation bindings.

    Behavior matches the previous server-side
    ``_populate_presentation_providers`` function exactly. Pre-Path-
    C, that function also synthesized a ``presentation-base`` →
    ``tailwind-default`` binding when none was declared. This
    function preserves that synthesis (the populator is advisory;
    deploy-time validation per BRD #2 §8.5 is the right place to
    fail-closed on missing required contracts). The synthesis
    no-ops if no ``tailwind-default`` factory is registered.
    """
    # Two locations where bindings might live, see BRD §11.2.
    flat = (deploy_config.get("bindings", {}) or {}).get("presentation", {})
    nested = (deploy_config.get("presentation", {}) or {}).get("bindings", {})
    bindings = {**(nested or {}), **(flat or {})}

    # Pre-existing convenience: when no presentation-base binding is
    # declared, synthesize one to tailwind-default so downstream
    # consumers (page_should_use_shell, the bundle-discovery
    # endpoint, conformance manifests) read a uniform shape whether
    # or not the deploy config names a provider.
    has_base_binding = (
        "presentation-base" in bindings
        or any(k.startswith("presentation-base.") for k in bindings)
    )
    if not has_base_binding:
        bindings = {
            **bindings,
            "presentation-base": {"provider": "tailwind-default", "config": {}},
        }
    if not bindings:
        return []

    instances: dict = {}  # product_name -> instance, cached across contracts

    def _get_or_create(product: str, config: dict):
        if product not in instances:
            for record in provider_registry.all_records():
                if (record.category == Category.PRESENTATION
                        and record.product_name == product):
                    instances[product] = record.factory(config or {})
                    break
        return instances.get(product)

    # Per-contract bindings first, then namespace fallback.
    pkg_registry = contract_package_registry
    contract_bindings: dict[str, dict] = {}
    for key, binding in bindings.items():
        if not isinstance(binding, dict):
            continue
        if "." in key:
            contract_bindings[key] = binding
            continue

        # Namespace binding — three expansion paths.
        full_names: tuple[str, ...] = ()
        if key == "presentation-base":
            full_names = tuple(
                f"presentation-base.{s}" for s in PRESENTATION_BASE_CONTRACTS
            )
        elif pkg_registry is not None and key in pkg_registry.namespaces():
            pkg = pkg_registry._packages.get(key)
            if pkg:
                full_names = tuple(f"{key}.{c.name}" for c in pkg.contracts)
        else:
            # v0.9.4 Path C fallback: instantiate the provider and
            # ask which contracts it declares in this namespace.
            # Quietly skip if the product isn't registered (deploy-
            # time validation is the right place to fail-closed).
            product = binding.get("provider")
            instance = (
                _get_or_create(product, binding.get("config") or {})
                if product else None
            )
            if instance is not None:
                declared = getattr(instance, "declared_contracts", ()) or ()
                prefix = f"{key}."
                full_names = tuple(c for c in declared if c.startswith(prefix))

        for full in full_names:
            contract_bindings.setdefault(full, binding)

    # Materialize: one (contract, product, instance) triple per
    # bound contract. Skip products with no registered factory.
    result: list[tuple[str, str, object]] = []
    for contract, binding in contract_bindings.items():
        product = binding.get("provider")
        if not product:
            continue
        instance = _get_or_create(product, binding.get("config") or {})
        if instance is None:
            continue
        result.append((contract, product, instance))
    return result


__all__ = ["build_presentation_provider_bindings"]
