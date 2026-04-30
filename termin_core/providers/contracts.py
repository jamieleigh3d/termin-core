# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Contract definitions and registry — v0.9 Phase 0.

Contracts describe the open surface providers implement. Phase 0
hardcodes the catalog of built-in contracts; the runtime ships with
exactly the contracts the BRD declares (one Identity, one Storage,
three Compute, four Channel, one Presentation).

Contract definitions are descriptive metadata in Phase 0 — they carry
the contract's name, category, tier classification, and naming kind
(implicit vs named). Phase 1+ adds operation signatures and
behavioral requirements; Phase 0 doesn't yet need them because no
primitive consults the registry to dispatch operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional


class Category(str, Enum):
    """The five primitive categories that have providers.

    String-valued so they serialize cleanly to JSON in deploy configs
    and conformance manifests.
    """
    IDENTITY = "identity"
    STORAGE = "storage"
    COMPUTE = "compute"
    CHANNELS = "channels"
    PRESENTATION = "presentation"


class Tier(int, Enum):
    """Operational tier per BRD §4.

    Tier 0: provider outage takes down the AppFabric. Identity only.
    Tier 1: provider outage takes down apps that depend on it.
    Tier 2: provider outage degrades a specific integration.
    """
    TIER_0 = 0
    TIER_1 = 1
    TIER_2 = 2


@dataclass(frozen=True)
class ContractDefinition:
    """Metadata describing one contract surface.

    name: the contract's identifier within its category. For implicit-
        naming categories this is "default"; for named categories it's
        the value referenced in source via Provider is "<name>".
    category: which primitive category.
    tier: operational tier.
    naming: "implicit" if source doesn't name the contract; "named"
        if source uses Provider is "<name>" to bind. Determines whether
        deploy config flat-binds the category or keys through contract
        names.
    description: short human-readable description.
    """
    name: str
    category: Category
    tier: Tier
    naming: str  # "implicit" | "named"
    description: str = ""

    def __post_init__(self) -> None:
        if self.naming not in ("implicit", "named"):
            raise ValueError(
                f"naming must be 'implicit' or 'named', got {self.naming!r}"
            )


# ── Built-in contract catalog ──
#
# Frozen at runtime. Phase 0 ships exactly these; new contracts are
# added by Termin spec evolution, not at runtime. Adoption happens
# through new providers against existing contracts (Tenet 4: providers
# over primitives).

_BUILTIN_CONTRACTS: tuple[ContractDefinition, ...] = (
    # Identity — single contract surface, implicit binding.
    ContractDefinition(
        name="default",
        category=Category.IDENTITY,
        tier=Tier.TIER_0,
        naming="implicit",
        description="Authentication + role resolution. Tier 0: AppFabric "
                    "depends on it. See BRD §6.1.",
    ),

    # Storage — single contract surface, implicit binding.
    ContractDefinition(
        name="default",
        category=Category.STORAGE,
        tier=Tier.TIER_1,
        naming="implicit",
        description="CRUD + predicate query + cascade-aware deletes + "
                    "schema migration. See BRD §6.2.",
    ),

    # Presentation — single contract surface, implicit binding.
    # Full treatment in BRD #2.
    ContractDefinition(
        name="default",
        category=Category.PRESENTATION,
        tier=Tier.TIER_1,
        naming="implicit",
        description="Component tree rendering. Three customization "
                    "levels deferred to Presentation BRD.",
    ),

    # Compute — three named contracts.
    ContractDefinition(
        name="default-CEL",
        category=Category.COMPUTE,
        tier=Tier.TIER_1,
        naming="named",
        description="Pure expression evaluation. Synchronous, "
                    "deterministic. Implicit when source has no "
                    "Provider is line. See BRD §6.3.1.",
    ),
    ContractDefinition(
        name="llm",
        category=Category.COMPUTE,
        tier=Tier.TIER_1,
        naming="named",
        description="Single-shot prompt → completion. No tool surface. "
                    "Streaming supported. See BRD §6.3.2.",
    ),
    ContractDefinition(
        name="ai-agent",
        category=Category.COMPUTE,
        tier=Tier.TIER_1,
        naming="named",
        description="Multi-action autonomous behavior with closed tool "
                    "surface. Streamable. See BRD §6.3.3.",
    ),

    # Channels — four named contracts.
    ContractDefinition(
        name="webhook",
        category=Category.CHANNELS,
        tier=Tier.TIER_2,
        naming="named",
        description="Outbound HTTP. Target URL in deploy config, "
                    "never source. See BRD §6.4.1.",
    ),
    ContractDefinition(
        name="email",
        category=Category.CHANNELS,
        tier=Tier.TIER_2,
        naming="named",
        description="Outbound email. SMTP credentials, default-from, "
                    "and recipients in deploy config. See BRD §6.4.2.",
    ),
    ContractDefinition(
        name="messaging",
        category=Category.CHANNELS,
        tier=Tier.TIER_2,
        naming="named",
        description="Chat platforms (Slack, Teams, Discord, etc.). "
                    "Target channel in deploy config. See BRD §6.4.3.",
    ),
    ContractDefinition(
        name="event-stream",
        category=Category.CHANNELS,
        tier=Tier.TIER_2,
        naming="named",
        description="Server-sent events / WebSocket for external "
                    "consumers. Internal Termin-to-Termin event "
                    "propagation uses the distributed runtime, not "
                    "this contract. See BRD §6.4.4.",
    ),
)


@dataclass
class ContractRegistry:
    """Catalog of contracts within each primitive category.

    Per BRD §4: primitives are closed (Tenet 4 audit promise — the
    eight primitive categories are fixed by core spec), but contracts
    within each category are semi-open. The reference runtime ships
    a fixed built-in catalog via `ContractRegistry.default()`, and
    providers can register new contracts within existing categories
    via `register_contract()`. New contracts add new shapes WITHIN a
    primitive — they don't extend the primitive itself, so the
    structural audit surface stays locked to the eight categories.

    Use cases for runtime-registered contracts:
      - A Carbon-style presentation provider registers a new
        presentation contract with a different rendering body shape.
      - A geospatial compute provider registers a new compute
        contract whose body lines have geospatial-specific syntax.

    The compiler delegates body-line verification to the provider's
    parser per BRD §5.3 (three-kinds-of-params model).
    """
    contracts: list[ContractDefinition] = field(default_factory=list)

    @classmethod
    def default(cls) -> "ContractRegistry":
        """Built-in catalog per BRD §4 and §6. Mutable — providers
        may extend via register_contract(). New ContractRegistry
        instances start fresh; default() always begins from the
        built-in baseline."""
        return cls(contracts=list(_BUILTIN_CONTRACTS))

    def register_contract(
        self,
        contract: ContractDefinition,
    ) -> None:
        """Register a new contract under an existing primitive category.

        Raises ValueError if a contract with the same (category, name)
        is already registered — providers should not silently shadow
        built-in contracts. To replace a built-in, the spec must
        evolve and ship a new release.
        """
        existing = self.get_contract(contract.category, contract.name)
        if existing is not None:
            raise ValueError(
                f"Contract ({contract.category.value}, {contract.name!r}) "
                f"is already registered. New contracts must use a unique "
                f"(category, name) within the registry; built-in "
                f"contracts cannot be redefined at runtime."
            )
        self.contracts.append(contract)

    def categories(self) -> Iterable[Category]:
        seen = set()
        for c in self.contracts:
            if c.category not in seen:
                seen.add(c.category)
                yield c.category

    def contracts_in(self, category: Category) -> tuple[ContractDefinition, ...]:
        return tuple(c for c in self.contracts if c.category == category)

    def get_contract(
        self, category: Category, name: str
    ) -> Optional[ContractDefinition]:
        for c in self.contracts:
            if c.category == category and c.name == name:
                return c
        return None

    def has_contract(self, category: Category, name: str) -> bool:
        return self.get_contract(category, name) is not None
