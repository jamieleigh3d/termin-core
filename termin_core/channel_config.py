# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Channel deploy configuration — loading, validation, and config types.

Handles loading app-specific deploy configs, resolving environment variables,
validating channel configurations against the IR, and defining config dataclasses.
"""

import json
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("termin.channels")


# ── Deploy config loading ──

def _resolve_env_vars(value):
    """Resolve ${VAR} placeholders in config values. Non-strings pass through."""
    if not isinstance(value, str):
        return value
    import re
    def _replace(m):
        var = m.group(1)
        return os.environ.get(var, m.group(0))
    return re.sub(r'\$\{(\w+)\}', _replace, value)


def _resolve_config_env(obj):
    """Recursively resolve env vars in a config dict."""
    if isinstance(obj, str):
        return _resolve_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _resolve_config_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_config_env(v) for v in obj]
    return obj


def load_deploy_config(path: str = None, app_name: str = None) -> dict:
    """Load app-specific deploy config from file.

    Search order:
      1. Explicit path (if provided)
      2. {app_name}.deploy.json in cwd
      3. deploy.json in cwd

    Returns empty dict if no config found.
    """
    candidates = []
    if path:
        candidates.append(Path(path))
    if app_name:
        candidates.append(Path(f"{app_name}.deploy.json"))
        # Also try variants that handle the name-vs-filename-stem
        # mismatch. `app_name` comes from the IR's `name` field
        # snake-cased (spaces/hyphens → underscores). The compiler
        # writes the deploy config file using the source-file stem,
        # which may not include the underscore-before-digit the
        # snake-case rule introduces. Example: "Agent Chatbot 2" ->
        # snake "agent_chatbot_2" but source stem "agent_chatbot2".
        # Try the collapsed variant as a fallback.
        import re
        collapsed = re.sub(r"_(\d)", r"\1", app_name)
        if collapsed != app_name:
            candidates.append(Path(f"{collapsed}.deploy.json"))
    candidates.append(Path("deploy.json"))

    for candidate in candidates:
        if candidate.exists():
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                resolved = _resolve_config_env(raw)
                logger.info(f"Loaded deploy config from {candidate}")
                return resolved
            except Exception as e:
                logger.warning(f"Failed to load deploy config {candidate}: {e}")
                return {}

    return {}


def check_deploy_config_warnings(deploy_config: dict, ir: dict) -> list[str]:
    """Check for unset environment variables and uncustomized placeholder values.

    Returns a list of warning messages.
    """
    warnings = []
    # v0.9: bindings live under bindings.channels
    channels_config = deploy_config.get("bindings", {}).get("channels", {})

    for ch in ir.get("channels", []):
        direction = ch.get("direction", "")
        if direction == "INTERNAL":
            continue
        display = ch["name"]["display"]
        snake = ch["name"]["snake"]
        ch_binding = channels_config.get(display) or channels_config.get(snake, {})
        if not ch_binding:
            continue

        # Check for unresolved ${ENV_VAR} patterns in the config subdict
        config_section = ch_binding.get("config", ch_binding)
        _check_unresolved_vars(config_section, f"bindings.channels.{display}", warnings)

    return warnings


def _check_unresolved_vars(obj, path: str, warnings: list):
    """Recursively check for unresolved ${VAR} and TODO-placeholder values."""
    import re
    if isinstance(obj, str):
        unresolved = re.findall(r'\$\{(\w+)\}', obj)
        for var in unresolved:
            warnings.append(f"Unresolved env var ${{{var}}} at {path}")
        # Detect TODO-style placeholder URLs/values (e.g., "https://TODO-...")
        if re.search(r'\bTODO\b', obj, re.IGNORECASE):
            warnings.append(f"Placeholder value at {path} still contains TODO — configure before deploying")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _check_unresolved_vars(v, f"{path}.{k}" if path else k, warnings)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_unresolved_vars(v, f"{path}[{i}]", warnings)


def validate_channel_config(ir: dict, deploy_config: dict) -> list[str]:
    """Validate that all external channels with a provider_contract have a binding.

    v0.9 Phase 4: checks bindings.channels for channels with provider_contract.
    Channels without provider_contract (inbound without contract, internal) are
    exempt. Returns list of error messages; empty = all OK.
    """
    errors = []
    # v0.9: bindings live under bindings.channels
    channels_config = deploy_config.get("bindings", {}).get("channels", {})

    for ch in ir.get("channels", []):
        direction = ch.get("direction", "")
        if direction == "INTERNAL":
            continue

        # Only validate channels that declare a provider_contract.
        # Inbound channels without a contract are exempt from the provider
        # requirement (analyzer TERMIN-S026 only fires for outbound/bidirectional).
        contract = ch.get("provider_contract")
        if not contract:
            continue

        display = ch["name"]["display"]
        snake = ch["name"]["snake"]

        binding = channels_config.get(display) or channels_config.get(snake)
        if not binding:
            errors.append(
                f"Channel '{display}' (contract: {contract}) has no deploy binding. "
                f"Add an entry under bindings.channels in the deploy config."
            )
            continue

        if not binding.get("provider"):
            errors.append(
                f"Channel '{display}' has a binding but no 'provider' key. "
                f"Specify the provider product (e.g. \"provider\": \"stub\")."
            )

    return errors


class ChannelConfigError(Exception):
    """Raised when channel configuration is invalid or missing."""
    pass


class ChannelError(Exception):
    """Base error for channel operations."""
    pass


class ChannelScopeError(ChannelError):
    """Caller lacks required scope for channel operation."""
    pass


class ChannelValidationError(ChannelError):
    """Invalid parameters for channel action."""
    pass


# ── Channel config types ──

@dataclass
class ChannelAuthConfig:
    """Authentication configuration for a channel."""
    auth_type: str = "none"         # bearer, api_key, mtls, oauth2, hmac, none
    token: str = ""
    header: str = "Authorization"
    secret: str = ""                # HMAC
    # Additional fields stored as extras
    extras: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ChannelAuthConfig":
        return cls(
            auth_type=d.get("type", "none"),
            token=d.get("token", ""),
            header=d.get("header", "Authorization"),
            secret=d.get("secret", ""),
            extras={k: v for k, v in d.items() if k not in ("type", "token", "header", "secret")},
        )


@dataclass
class ChannelConfig:
    """Deployment configuration for a single channel."""
    url: str = ""
    protocol: str = "http"          # http, websocket, grpc
    auth: ChannelAuthConfig = field(default_factory=ChannelAuthConfig)
    timeout_ms: int = 30000
    max_retries: int = 3
    backoff_ms: int = 1000
    reconnect: bool = True
    heartbeat_ms: int = 30000

    @classmethod
    def from_dict(cls, d: dict) -> "ChannelConfig":
        auth = ChannelAuthConfig.from_dict(d.get("auth", {}))
        retry = d.get("retry", {})
        return cls(
            url=d.get("url", ""),
            protocol=d.get("protocol", "http"),
            auth=auth,
            timeout_ms=d.get("timeout_ms", 30000),
            max_retries=retry.get("max_attempts", 3),
            backoff_ms=retry.get("backoff_ms", 1000),
            reconnect=d.get("reconnect", True),
            heartbeat_ms=d.get("heartbeat_ms", 30000),
        )
