# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Provider registry — v0.9 Phase 0.

Providers register themselves against a (category, contract_name,
product_name) key. Phase 0 ships an empty registry; first-party
providers register in Phase 1+ through the same path third-party
providers will use. No special-cased "built-in" path.

The registry holds factory functions, not instantiated providers —
the runtime decides when to construct a provider instance using the
deploy config's resolved bindings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .contracts import Category, ContractRegistry


# A factory function takes a config dict (provider-specific) and
# returns the provider instance. Phase 0 doesn't constrain the return
# type because no primitive consults the registry yet; Phase 1+ will
# add per-category Protocol types so factories return type-checkable
# provider implementations.
ProviderFactory = Callable[[dict], object]


@dataclass(frozen=True)
class ProviderRecord:
    """One registered provider.

    category: which primitive category it implements.
    contract_name: which contract within that category.
    product_name: the provider's product identifier (e.g., "stub",
        "okta", "anthropic", "slack"). Globally unique within
        (category, contract_name).
    factory: callable that constructs the provider given a config dict.
    conformance: "passing" | "partial" | "failing" — how the provider
        scores against the contract's conformance test pack. Per
        BRD §9.2 conformance manifest.
    version: provider version string.
    features: optional list of sub-features the provider implements
        (e.g., a messaging provider may support "send_message" and
        "react" but not "thread_reply").
    """
    category: Category
    contract_name: str
    product_name: str
    factory: ProviderFactory
    conformance: str = "passing"
    version: str = ""
    features: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class ProviderRegistry:
    """Registry of available providers.

    Empty by default in Phase 0. First-party providers register
    through register() during Phase 1+. The runtime may also expose
    a Python entry-point mechanism in a future phase for third-party
    discovery; for Phase 0 explicit registration is the only path.

    The registry indexes by (category, contract_name, product_name).
    Multiple products may implement the same contract — the deploy
    config picks which one to bind for a given app/boundary.
    """
    _providers: dict = field(default_factory=dict)

    def register(
        self,
        category: Category,
        contract_name: str,
        product_name: str,
        factory: ProviderFactory,
        conformance: str = "passing",
        version: str = "",
        features: Optional[list] = None,
        contract_registry: Optional[ContractRegistry] = None,
    ) -> ProviderRecord:
        """Register a provider against a contract.

        If contract_registry is supplied, validates that the
        (category, contract_name) pair is a known contract — prevents
        typos like registering against "ai_agent" instead of "ai-agent".
        Phase 1+ should always pass the contract registry.
        """
        if contract_registry is not None:
            if not contract_registry.has_contract(category, contract_name):
                raise ValueError(
                    f"Cannot register provider {product_name!r}: contract "
                    f"({category.value}, {contract_name!r}) is not known. "
                    f"See ContractRegistry.default() for the catalog."
                )

        record = ProviderRecord(
            category=category,
            contract_name=contract_name,
            product_name=product_name,
            factory=factory,
            conformance=conformance,
            version=version,
            features=tuple(features or ()),
        )
        key = (category, contract_name, product_name)
        self._providers[key] = record
        return record

    def get(
        self, category: Category, contract_name: str, product_name: str
    ) -> Optional[ProviderRecord]:
        """Retrieve a registered provider by full key. None if absent."""
        return self._providers.get((category, contract_name, product_name))

    def list_products(
        self, category: Category, contract_name: str
    ) -> list[str]:
        """All product names registered for a (category, contract) pair."""
        return [
            key[2] for key in self._providers
            if key[0] == category and key[1] == contract_name
        ]

    def all_records(self) -> list[ProviderRecord]:
        """Every registered provider (for diagnostics / manifest export)."""
        return list(self._providers.values())

    def clear(self) -> None:
        """Empty the registry. Test helper; production code should not
        call this — register() is additive over the runtime's lifetime."""
        self._providers.clear()
