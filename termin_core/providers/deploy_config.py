# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""v0.9 deploy config schema parser.

Parses *.deploy.json content (or an equivalent dict) into a typed
DeployConfig structure that the rest of the runtime can consume
without re-validating the shape.

Schema invariants (BRD §7):
  - Top-level: {version, bindings, runtime}
  - bindings has exactly five categories: identity, storage,
    presentation, compute, channels (each present, may be empty)
  - identity / storage / presentation are flat-bound:
    {provider, config[, role_mappings]}
  - compute / channels are keyed-by-name:
    {<key>: {provider, config}}
  - Env-var interpolation in config values (e.g., ${API_KEY}) is
    preserved verbatim — runtime resolves at provider construction
    time, not at parse time. Phase 0 doesn't yet do the resolution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Union


_TOP_LEVEL_REQUIRED = ("version", "bindings", "runtime")
_TOP_LEVEL_ALLOWED = set(_TOP_LEVEL_REQUIRED)
_BINDING_CATEGORIES_REQUIRED = (
    "identity", "storage", "presentation", "compute", "channels",
)
_BINDING_CATEGORIES_ALLOWED = set(_BINDING_CATEGORIES_REQUIRED)


class DeployConfigError(ValueError):
    """Raised on any v0.9 deploy config validation failure.

    Subclass of ValueError so callers that catch ValueError still
    work, but distinguished for tooling that wants to surface
    deploy-config errors specifically.
    """


@dataclass(frozen=True)
class IdentityBinding:
    """identity flat-binding: provider + config + role_mappings."""
    provider: str
    config: dict = field(default_factory=dict)
    role_mappings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StorageBinding:
    """storage flat-binding: provider + config."""
    provider: str
    config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PresentationBinding:
    """presentation flat-binding: provider + config."""
    provider: str
    config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NamedBinding:
    """compute / channels keyed-by-name binding: provider + config."""
    provider: str
    config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Bindings:
    """The five-category bindings block."""
    identity: IdentityBinding
    storage: StorageBinding
    presentation: PresentationBinding
    compute: dict        # name -> NamedBinding
    channels: dict       # name -> NamedBinding


@dataclass(frozen=True)
class DeployConfig:
    """A parsed and validated v0.9 deploy config."""
    version: str
    bindings: Bindings
    runtime: dict = field(default_factory=dict)


# ── Parsing entry point ──


def parse_deploy_config(source: Union[dict, str]) -> DeployConfig:
    """Parse a v0.9 deploy config from a dict or JSON string.

    Raises DeployConfigError on any shape, missing-key, or unknown-key
    violation. Errors include the offending key/path to help the user
    locate the problem.
    """
    if isinstance(source, str):
        try:
            data = json.loads(source)
        except json.JSONDecodeError as e:
            raise DeployConfigError(
                f"Failed to parse deploy config JSON: {e}"
            ) from e
    elif isinstance(source, dict):
        data = source
    else:
        raise DeployConfigError(
            f"parse_deploy_config requires dict or str, got {type(source).__name__}"
        )

    _validate_top_level(data)
    bindings = _parse_bindings(data["bindings"])
    return DeployConfig(
        version=str(data["version"]),
        bindings=bindings,
        runtime=dict(data.get("runtime", {})),
    )


# ── Internal validation ──


def _validate_top_level(data: dict) -> None:
    for k in _TOP_LEVEL_REQUIRED:
        if k not in data:
            raise DeployConfigError(
                f"Missing required top-level key: {k!r}. "
                f"v0.9 deploy configs must have {_TOP_LEVEL_REQUIRED}."
            )
    for k in data:
        if k not in _TOP_LEVEL_ALLOWED:
            raise DeployConfigError(
                f"Unknown top-level key: {k!r}. "
                f"v0.9 allows only {sorted(_TOP_LEVEL_ALLOWED)}."
            )


def _parse_bindings(raw: dict) -> Bindings:
    if not isinstance(raw, dict):
        raise DeployConfigError(
            f"bindings must be a dict, got {type(raw).__name__}"
        )
    for k in _BINDING_CATEGORIES_REQUIRED:
        if k not in raw:
            raise DeployConfigError(
                f"Missing required binding category: bindings.{k}. "
                f"All five categories must be present (may be empty)."
            )
    for k in raw:
        if k not in _BINDING_CATEGORIES_ALLOWED:
            raise DeployConfigError(
                f"Unknown binding category: bindings.{k!r}. "
                f"v0.9 allows only {sorted(_BINDING_CATEGORIES_ALLOWED)}."
            )
    return Bindings(
        identity=_parse_identity(raw["identity"]),
        storage=_parse_storage(raw["storage"]),
        presentation=_parse_presentation(raw["presentation"]),
        compute=_parse_named_map(raw["compute"], "compute"),
        channels=_parse_named_map(raw["channels"], "channels"),
    )


def _parse_identity(raw: dict) -> IdentityBinding:
    if not isinstance(raw, dict):
        raise DeployConfigError(
            f"bindings.identity must be a dict, got {type(raw).__name__}"
        )
    if "provider" not in raw:
        raise DeployConfigError(
            "bindings.identity is missing required 'provider' key."
        )
    return IdentityBinding(
        provider=str(raw["provider"]),
        config=dict(raw.get("config", {})),
        role_mappings=dict(raw.get("role_mappings", {})),
    )


def _parse_storage(raw: dict) -> StorageBinding:
    if not isinstance(raw, dict):
        raise DeployConfigError(
            f"bindings.storage must be a dict, got {type(raw).__name__}"
        )
    if "provider" not in raw:
        raise DeployConfigError(
            "bindings.storage is missing required 'provider' key."
        )
    return StorageBinding(
        provider=str(raw["provider"]),
        config=dict(raw.get("config", {})),
    )


def _parse_presentation(raw: dict) -> PresentationBinding:
    if not isinstance(raw, dict):
        raise DeployConfigError(
            f"bindings.presentation must be a dict, got {type(raw).__name__}"
        )
    if "provider" not in raw:
        raise DeployConfigError(
            "bindings.presentation is missing required 'provider' key."
        )
    return PresentationBinding(
        provider=str(raw["provider"]),
        config=dict(raw.get("config", {})),
    )


def _parse_named_map(raw: dict, label: str) -> dict:
    """Parse a compute / channels entry — keyed-by-name dict of bindings."""
    if not isinstance(raw, dict):
        raise DeployConfigError(
            f"bindings.{label} must be a dict, got {type(raw).__name__}"
        )
    out: dict = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            raise DeployConfigError(
                f"bindings.{label}.{key!r} must be a dict, "
                f"got {type(entry).__name__}"
            )
        if "provider" not in entry:
            raise DeployConfigError(
                f"bindings.{label}.{key!r} is missing required "
                f"'provider' key."
            )
        out[key] = NamedBinding(
            provider=str(entry["provider"]),
            config=dict(entry.get("config", {})),
        )
    return out
