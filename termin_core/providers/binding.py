# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Effective-binding resolver — v0.9 Phase 0.

Per BRD §8: an application's effective deploy config is computed by
key-level shallow merging the configs along the boundary tree from
root down to the leaf application. Leaf wins at every key it
specifies; keys absent at the leaf inherit from the parent.

The merge is shallow at every level. A nested config sub-object
(like 'auth: {...}') replaces wholesale rather than merging key-by-key
inside. Reviewers can diff one boundary level at a time and see
exactly what each contributes.
"""

from __future__ import annotations

from typing import Iterable

from .deploy_config import (
    DeployConfig, Bindings, IdentityBinding, StorageBinding,
    PresentationBinding, NamedBinding,
)


def resolve_effective_bindings(chain: Iterable[DeployConfig]) -> DeployConfig:
    """Merge a chain of [root, ..., parent, leaf] into one
    effective DeployConfig.

    Order matters: earliest = root, latest = leaf. Last config wins
    on every conflict per the BRD §8 shallow-merge rule.

    Raises ValueError if the chain is empty or if configs have
    inconsistent versions (a likely misconfiguration that shouldn't
    be silently merged).
    """
    chain = list(chain)
    if not chain:
        raise ValueError(
            "resolve_effective_bindings requires at least one config in the chain"
        )

    versions = {c.version for c in chain}
    if len(versions) > 1:
        raise ValueError(
            f"DeployConfig version mismatch in boundary chain: {sorted(versions)}. "
            f"All levels must use the same schema version."
        )
    version = chain[0].version

    # Start from root, fold each subsequent config on top.
    effective = chain[0]
    for cfg in chain[1:]:
        effective = _merge_two(effective, cfg)
    # The merge function carries version through unchanged; all configs
    # share one version per the check above.
    return DeployConfig(
        version=version,
        bindings=effective.bindings,
        runtime=effective.runtime,
    )


# ── Internal: pairwise merge ──


def _merge_two(parent: DeployConfig, leaf: DeployConfig) -> DeployConfig:
    """Merge leaf on top of parent. Leaf wins per the shallow rule."""
    return DeployConfig(
        version=leaf.version,  # versions verified equal at top level
        bindings=Bindings(
            identity=_merge_identity(parent.bindings.identity, leaf.bindings.identity),
            storage=_merge_storage(parent.bindings.storage, leaf.bindings.storage),
            presentation=_merge_presentation(
                parent.bindings.presentation, leaf.bindings.presentation
            ),
            compute=_merge_named_map(parent.bindings.compute, leaf.bindings.compute),
            channels=_merge_named_map(parent.bindings.channels, leaf.bindings.channels),
        ),
        runtime=_shallow_merge(parent.runtime, leaf.runtime),
    )


def _merge_identity(parent: IdentityBinding, leaf: IdentityBinding) -> IdentityBinding:
    return IdentityBinding(
        provider=leaf.provider,  # leaf binding always wins on provider
        config=_shallow_merge(parent.config, leaf.config),
        role_mappings=_shallow_merge(parent.role_mappings, leaf.role_mappings),
    )


def _merge_storage(parent: StorageBinding, leaf: StorageBinding) -> StorageBinding:
    return StorageBinding(
        provider=leaf.provider,
        config=_shallow_merge(parent.config, leaf.config),
    )


def _merge_presentation(
    parent: PresentationBinding, leaf: PresentationBinding
) -> PresentationBinding:
    return PresentationBinding(
        provider=leaf.provider,
        config=_shallow_merge(parent.config, leaf.config),
    )


def _merge_named_map(parent: dict, leaf: dict) -> dict:
    """compute / channels keyed-by-name merge.

    Keys from both sides are present in the result. Where the same
    key appears in both, leaf wins on provider; configs shallow-merge.
    """
    out: dict = {}
    # Start from parent keys.
    for key, parent_binding in parent.items():
        if key not in leaf:
            out[key] = parent_binding
        else:
            leaf_binding = leaf[key]
            out[key] = NamedBinding(
                provider=leaf_binding.provider,
                config=_shallow_merge(parent_binding.config, leaf_binding.config),
            )
    # Add keys only in leaf.
    for key, leaf_binding in leaf.items():
        if key not in parent:
            out[key] = leaf_binding
    return out


def _shallow_merge(parent: dict, leaf: dict) -> dict:
    """Shallow dict merge: leaf keys overlay parent keys.

    Values at conflicting keys are NOT recursively merged — leaf's
    value replaces parent's value wholesale. This is the BRD §8
    "shallow at every level" rule.
    """
    out = dict(parent)
    out.update(leaf)
    return out
