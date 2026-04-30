# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Termin v0.9 provider system — Phase 0 scaffolding.

This package adds the contract registry, provider registry, deploy
config schema, and effective-binding resolver. It does NOT change any
primitive behavior in Phase 0; existing runtime modules (identity,
storage, app, routes, compute_runner, channels) continue to work
unchanged. Phase 1+ wires individual primitives to the registry.

Public surface:
  - Category, Tier — enums
  - ContractDefinition — describes one contract surface
  - ContractRegistry — catalog of available contracts (fixed in Phase 0)
  - ProviderRecord — describes one registered provider
  - ProviderRegistry — registry of registered providers (empty in Phase 0)
  - DeployConfig — parsed v0.9 deploy config
  - parse_deploy_config — JSON/dict → DeployConfig
  - EffectiveBindings, resolve_effective_bindings — boundary-tree merger

See docs/termin-provider-system-brd-v0.9.md for the full spec.
"""

from .contracts import (
    Category, Tier, ContractDefinition, ContractRegistry,
)
from .registry import ProviderRecord, ProviderRegistry
from .deploy_config import (
    DeployConfig, Bindings, IdentityBinding, StorageBinding,
    PresentationBinding, NamedBinding, DeployConfigError,
    parse_deploy_config,
)
from .binding import resolve_effective_bindings
from .identity_contract import (
    Principal, RoleName, IdentityProvider, ANONYMOUS_PRINCIPAL,
)
from .storage_contract import (
    StorageProvider, Predicate, Eq, Ne, Gt, Gte, Lt, Lte, In, Contains,
    And, Or, Not, OrderBy, QueryOptions, Page, CascadeMode,
    FieldChange, ContentChange, MigrationDiff, initial_deploy_diff,
    CLASSIFICATIONS, worst_classification,
    BackupFailedError, MigrationValidationError, ProviderInjectedFault,
    UpdateResult,
)
from .compute_contract import (
    DefaultCelComputeProvider, LlmComputeProvider, AiAgentComputeProvider,
    CompletionResult, AgentResult, AgentContext, AuditableAction,
    ToolSurface, AuditRecord, ToolCall, Cost,
    AgentEvent, TokenEmitted, ToolCalled, ToolResult, Completed, Failed,
    ToolNotDeclared, NotAuthorized,
)

__all__ = [
    "Category", "Tier", "ContractDefinition", "ContractRegistry",
    "ProviderRecord", "ProviderRegistry",
    "DeployConfig", "Bindings", "IdentityBinding", "StorageBinding",
    "PresentationBinding", "NamedBinding", "DeployConfigError",
    "parse_deploy_config",
    "resolve_effective_bindings",
    "Principal", "RoleName", "IdentityProvider", "ANONYMOUS_PRINCIPAL",
    "StorageProvider", "Predicate",
    "Eq", "Ne", "Gt", "Gte", "Lt", "Lte", "In", "Contains",
    "And", "Or", "Not", "OrderBy", "QueryOptions", "Page", "CascadeMode",
    "FieldChange", "ContentChange", "MigrationDiff", "initial_deploy_diff",
    "CLASSIFICATIONS", "worst_classification",
    "BackupFailedError", "MigrationValidationError", "ProviderInjectedFault",
    "UpdateResult",
    "DefaultCelComputeProvider", "LlmComputeProvider", "AiAgentComputeProvider",
    "CompletionResult", "AgentResult", "AgentContext", "AuditableAction",
    "ToolSurface", "AuditRecord", "ToolCall", "Cost",
    "AgentEvent", "TokenEmitted", "ToolCalled", "ToolResult", "Completed",
    "Failed",
    "ToolNotDeclared", "NotAuthorized",
]
