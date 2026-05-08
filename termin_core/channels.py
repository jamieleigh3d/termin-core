# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Channel dispatcher — connects declared Channels to external services.

Reads channel declarations from the IR and connection config from
termin.deploy.json. Provides send/invoke/receive operations with
scope enforcement, type validation, and delivery semantics.

Subsystem modules:
  - channel_config.py: Deploy config loading, validation, config types
  - channel_ws.py: Outbound WebSocket connection with auto-reconnect
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

import httpx

from .channel_config import (
    load_deploy_config, check_deploy_config_warnings, validate_channel_config,
    ChannelConfigError, ChannelAuthConfig, ChannelConfig,
    ChannelError, ChannelScopeError, ChannelValidationError,
    _resolve_env_vars, _resolve_config_env, _check_unresolved_vars,
)
from .channel_ws import WebSocketConnection
from .providers import Category, ProviderRegistry

logger = logging.getLogger("termin.channels")


# ── Channel dispatcher ──

class ChannelDispatcher:
    """Manages connections to external services via declared Channels.

    Reads channel specs from the IR and connection details from deploy config.
    Provides send (data), invoke (action), and receive (inbound) operations.
    Supports HTTP (reliable) and WebSocket (realtime) protocols.
    """

    def __init__(self, ir: dict, deploy_config: dict = None,
                 provider_registry: Optional[ProviderRegistry] = None):
        self._ir = ir
        self._deploy = deploy_config or {}
        self._registry = provider_registry
        self._channel_specs: dict[str, dict] = {}   # snake_name -> IR channel spec
        self._channel_configs: dict[str, ChannelConfig] = {}  # display_name -> config
        self._channel_providers: dict[str, Any] = {}  # v0.9 Phase 4: display -> provider instance
        self._http_client: Optional[httpx.AsyncClient] = None
        self._ws_connections: dict[str, WebSocketConnection] = {}  # display_name -> connection
        self._message_handlers: list[Callable] = []  # callbacks for inbound WS messages
        self._metrics: dict[str, dict] = {}  # channel_name -> {sent, received, errors, last_active, state}

        # Index channel specs by both display and snake names.
        # v0.9 Phase 4: channel bindings live exclusively under
        # bindings.channels (v0.8 top-level fallback removed).
        # Channels with provider_contract use _channel_providers (set
        # at startup); channels without use _channel_configs (URL path).
        bindings = self._deploy.get("bindings", {}).get("channels", {})
        for ch in ir.get("channels", []):
            display = ch["name"]["display"]
            snake = ch["name"]["snake"]
            self._channel_specs[snake] = ch
            self._channel_specs[display] = ch
            self._metrics[display] = {
                "sent": 0, "received": 0, "errors": 0,
                "last_active": None, "state": "disconnected",
            }

            # Only build ChannelConfig for channels without a provider_contract
            # (WebSocket / legacy URL channels). Provider-contract channels are
            # wired up in startup() via the ProviderRegistry.
            if not ch.get("provider_contract"):
                raw = bindings.get(display) or bindings.get(snake)
                if raw and isinstance(raw, dict) and raw.get("url"):
                    self._channel_configs[display] = ChannelConfig.from_dict(raw)

    def on_ws_message(self, handler: Callable[[str, dict], Coroutine]):
        """Register a callback for inbound WebSocket messages."""
        self._message_handlers.append(handler)

    async def _dispatch_ws_message(self, channel_name: str, data: dict):
        """Route an inbound WebSocket message to all registered handlers."""
        display = channel_name
        self._metrics.get(display, {})["received"] = \
            self._metrics.get(display, {}).get("received", 0) + 1
        self._metrics.get(display, {})["last_active"] = _now_iso()

        for handler in self._message_handlers:
            try:
                await handler(channel_name, data)
            except Exception as e:
                logger.error(f"Channel '{channel_name}': message handler error: {e}")

    def validate(self) -> list[str]:
        """Validate that all non-internal channels have deploy config."""
        return validate_channel_config(self._ir, self._deploy)

    async def startup(self, strict: bool = True):
        """Initialize HTTP client, wire channel providers, and connect WebSockets."""
        # v0.9 Phase 4: resolve provider-contract channels via the registry.
        # Must happen before the strict validation check so that missing
        # bindings surface as ChannelConfigError when strict=True.
        if self._registry is not None:
            for ch in self._ir.get("channels", []):
                display = ch["name"]["display"]
                contract = ch.get("provider_contract")
                if not contract:
                    continue  # internal or legacy URL channel — handled below

                binding = self._get_channel_binding(display)
                if binding is None:
                    if strict:
                        raise ChannelConfigError(
                            f"Channel '{display}' (contract: {contract}) has no deploy binding. "
                            f"Add an entry to bindings.channels in the deploy config."
                        )
                    logger.info(
                        f"Channel '{display}': no binding in deploy config, "
                        f"log-and-drop mode active"
                    )
                    continue

                product = binding.get("provider", "stub")
                config_dict = binding.get("config") or {}
                record = self._registry.get(Category.CHANNELS, contract, product)
                if record is None:
                    # Try stub fallback for the same contract
                    record = self._registry.get(Category.CHANNELS, contract, "stub")
                if record is not None:
                    self._channel_providers[display] = record.factory(config_dict)
                    logger.info(
                        f"Channel '{display}': wired to {contract!r}/{product!r} provider"
                    )
                else:
                    logger.warning(
                        f"Channel '{display}': no provider registered for "
                        f"contract={contract!r}, product={product!r}. Log-and-drop active."
                    )
        elif strict:
            errors = self.validate()
            if errors:
                raise ChannelConfigError(
                    f"Cannot start application — {len(errors)} channel(s) missing deploy config:\n"
                    + "\n".join(f"  - {e}" for e in errors)
                )

        self._http_client = httpx.AsyncClient(timeout=60.0)

        # Connect WebSocket channels (legacy URL path — channels without provider_contract)
        for ch in self._ir.get("channels", []):
            display = ch["name"]["display"]
            if ch.get("provider_contract"):
                continue  # handled above via provider registry
            config = self._channel_configs.get(display)
            if not config or config.protocol != "websocket":
                continue

            direction = ch.get("direction", "")
            if direction in ("OUTBOUND", "BIDIRECTIONAL"):
                ws_conn = WebSocketConnection(
                    display, config,
                    on_message=self._dispatch_ws_message,
                )
                self._ws_connections[display] = ws_conn
                try:
                    await ws_conn.connect()
                    self._metrics[display]["state"] = ws_conn.state
                except Exception as e:
                    logger.error(f"Channel '{display}': initial WebSocket connect failed: {e}")
                    self._metrics[display]["state"] = "error"

    async def shutdown(self):
        """Close HTTP client and all WebSocket connections."""
        for name, ws_conn in self._ws_connections.items():
            await ws_conn.close()
            self._metrics.get(name, {})["state"] = "disconnected"
        self._ws_connections.clear()

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    def get_spec(self, channel_name: str) -> Optional[dict]:
        """Get IR spec for a channel by display or snake name."""
        return self._channel_specs.get(channel_name)

    def get_config(self, channel_name: str) -> Optional[ChannelConfig]:
        """Get deploy config for a channel."""
        if channel_name in self._channel_configs:
            return self._channel_configs[channel_name]
        spec = self._channel_specs.get(channel_name)
        if spec:
            display = spec["name"]["display"]
            return self._channel_configs.get(display)
        return None

    def _build_headers(self, config: ChannelConfig) -> dict:
        """Build HTTP headers from auth config."""
        headers = {"Content-Type": "application/json"}
        auth = config.auth
        if auth.auth_type == "bearer" and auth.token:
            headers["Authorization"] = f"Bearer {auth.token}"
        elif auth.auth_type == "api_key" and auth.token:
            headers[auth.header] = auth.token
        elif auth.auth_type == "none":
            first_role = ""
            for r in self._ir.get("auth", {}).get("roles", []):
                first_role = r.get("name", "")
                break
            if first_role:
                headers["Cookie"] = f"termin_role={first_role}; termin_user_name=channel-dispatcher"
        return headers

    def _check_scope(self, channel_name: str, direction: str, user_scopes: set) -> bool:
        """Check if user has required scope for a channel operation."""
        spec = self.get_spec(channel_name)
        if not spec:
            return False
        for req in spec.get("requirements", []):
            if req["direction"] == direction:
                if req["scope"] not in user_scopes:
                    return False
        return True

    def _check_action_scope(self, channel_name: str, action_name: str, user_scopes: set) -> bool:
        """Check if user has required scope for an action invocation."""
        spec = self.get_spec(channel_name)
        if not spec:
            return False
        for action in spec.get("actions", []):
            if action["name"]["display"] == action_name or action["name"]["snake"] == action_name:
                for scope in action.get("required_scopes", []):
                    if scope not in user_scopes:
                        return False
                return True
        return False

    def get_action_spec(self, channel_name: str, action_name: str) -> Optional[dict]:
        """Get the IR spec for a specific action on a channel."""
        spec = self.get_spec(channel_name)
        if not spec:
            return None
        for action in spec.get("actions", []):
            if action["name"]["display"] == action_name or action["name"]["snake"] == action_name:
                return action
        return None

    # ── Send (data channels) ──

    def _get_channel_binding(self, display: str) -> Optional[dict]:
        """Return the v0.9 bindings.channels entry for a channel, or None."""
        return self._deploy.get("bindings", {}).get("channels", {}).get(display)

    def _resolve_messaging_target(self, display: str) -> str:
        """Resolve the messaging target name from the channel binding config."""
        binding = self._get_channel_binding(display)
        if binding:
            return binding.get("config", {}).get("target", "stub-channel")
        return "stub-channel"

    async def _dispatch_send(self, display: str, spec: dict, provider: Any, data: dict) -> dict:
        """Route a data-channel send to the correct provider method.

        v0.9 Phase 4 contracts supported:
          webhook  — provider.send(body)
          email    — provider.send(recipients, subject, body, ...)
          messaging — provider.send(target, message_text)

        Always returns {"ok": bool, "outcome": str, "channel": display}.
        """
        contract = spec.get("provider_contract", "")
        try:
            if contract == "webhook":
                result = await provider.send(body=data)
                return {
                    "ok": result.outcome == "delivered",
                    "outcome": result.outcome,
                    "channel": display,
                }
            elif contract == "email":
                result = await provider.send(
                    recipients=data.get("recipients", []),
                    subject=data.get("subject", ""),
                    body=data.get("body", str(data)),
                    html_body=data.get("html_body"),
                )
                return {
                    "ok": result.outcome == "delivered",
                    "outcome": result.outcome,
                    "channel": display,
                }
            elif contract == "messaging":
                target = data.get("target") or self._resolve_messaging_target(display)
                msg_ref = await provider.send(
                    target=target,
                    message_text=data.get("text", str(data)),
                )
                return {
                    "ok": True,
                    "outcome": "delivered",
                    "channel": display,
                    "message_ref": msg_ref.id,
                }
            else:
                raise ChannelError(f"Unknown provider contract {contract!r} on channel '{display}'")
        except ChannelError:
            raise
        except Exception as e:
            # Failure mode resolution per BRD §6.4.5:
            #   log-and-drop (default) — log + return outcome=failed; never raise.
            #   surface-as-error       — re-raise as ChannelError to the caller.
            #   queue-and-retry        — grammar placeholder in v0.9.x; full
            #                            implementation (exp backoff +
            #                            dead-letter, max 24h) lands v0.10.
            #                            v0.9.1 falls back to log-and-drop.
            failure_mode = spec.get("failure_mode", "log-and-drop")
            self._metrics[display]["errors"] += 1
            if failure_mode == "surface-as-error":
                logger.warning(
                    f"Channel '{display}' send failed ({contract}, "
                    f"surface-as-error): {e}"
                )
                raise ChannelError(
                    f"Channel '{display}' send failed: {e}"
                ) from e
            if failure_mode == "queue-and-retry":
                logger.warning(
                    f"Channel '{display}' send failed ({contract}, "
                    f"queue-and-retry placeholder — falling back to "
                    f"log-and-drop pending v0.10 worker): {e}"
                )
            else:
                logger.warning(
                    f"Channel '{display}' send failed ({contract}): {e}"
                )
            return {"ok": False, "outcome": "failed", "channel": display}

    async def channel_send(self, channel_name: str, data: dict, user_scopes: set = None) -> dict:
        """Send data through an outbound data channel.

        v0.9 Phase 4: channels with provider_contract route through the provider
        registry. Channels without (internal / legacy URL) fall through to the
        existing WebSocket / HTTP paths.
        """
        spec = self.get_spec(channel_name)
        if not spec:
            raise ChannelError(f"Unknown channel: {channel_name}")

        display = spec["name"]["display"]

        if user_scopes is not None and not self._check_scope(channel_name, "send", user_scopes):
            raise ChannelScopeError(f"Insufficient scope to send on channel '{display}'")

        # v0.9 Phase 4: route through provider registry if available
        provider = self._channel_providers.get(display)
        if spec.get("provider_contract"):
            if provider is None:
                logger.info(
                    f"Channel '{display}': no provider registered, send skipped (log-and-drop)"
                )
                return {"ok": True, "status": "not_configured", "channel": display}
            self._metrics[display]["sent"] += 1
            self._metrics[display]["last_active"] = _now_iso()
            return await self._dispatch_send(display, spec, provider, data)

        # Legacy URL / WebSocket path for channels without provider_contract
        config = self.get_config(channel_name)
        if not config or not config.url:
            logger.info(f"Channel '{display}': no deploy config, send skipped")
            self._metrics[display]["sent"] += 1
            return {"ok": True, "status": "not_configured", "channel": display}

        if config.protocol == "websocket" and display in self._ws_connections:
            return await self._ws_send(display, data)
        else:
            return await self._http_send(display, config, data)

    async def _ws_send(self, display: str, data: dict) -> dict:
        """Send data over an outbound WebSocket connection."""
        ws_conn = self._ws_connections.get(display)
        if not ws_conn or ws_conn.state != "connected":
            self._metrics[display]["errors"] += 1
            raise ChannelError(f"Channel '{display}': WebSocket not connected")
        try:
            await ws_conn.send(data)
            self._metrics[display]["sent"] += 1
            self._metrics[display]["last_active"] = _now_iso()
            return {"ok": True, "status": "sent", "channel": display, "protocol": "websocket"}
        except ChannelError:
            self._metrics[display]["errors"] += 1
            raise

    async def _http_send(self, display: str, config: ChannelConfig, data: dict) -> dict:
        """Send data over HTTP with retry."""
        headers = self._build_headers(config)
        url = config.url
        last_error = None

        for attempt in range(config.max_retries + 1):
            try:
                response = await self._http_client.post(
                    url, json=data, headers=headers,
                    timeout=config.timeout_ms / 1000.0,
                )
                self._metrics[display]["sent"] += 1
                self._metrics[display]["last_active"] = _now_iso()

                if response.status_code < 400:
                    return {"ok": True, "status": response.status_code, "channel": display}
                else:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    if response.status_code < 500:
                        break
            except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as e:
                last_error = str(e)

            if attempt < config.max_retries:
                backoff = config.backoff_ms * (2 ** attempt) / 1000.0
                await asyncio.sleep(backoff)

        self._metrics[display]["errors"] += 1
        raise ChannelError(f"Channel '{display}' send failed after {config.max_retries + 1} attempts: {last_error}")

    # ── Invoke (action channels) ──

    async def channel_invoke(
        self, channel_name: str, action_name: str, params: dict, user_scopes: set = None
    ) -> dict:
        """Invoke a typed action on a channel."""
        spec = self.get_spec(channel_name)
        if not spec:
            raise ChannelError(f"Unknown channel: {channel_name}")

        display = spec["name"]["display"]
        action_spec = self.get_action_spec(channel_name, action_name)
        if not action_spec:
            raise ChannelError(f"Unknown action '{action_name}' on channel '{display}'")

        action_display = action_spec["name"]["display"]
        action_snake = action_spec["name"]["snake"]

        if user_scopes is not None and not self._check_action_scope(channel_name, action_name, user_scopes):
            raise ChannelScopeError(f"Insufficient scope to invoke '{action_display}' on channel '{display}'")

        for param_spec in action_spec.get("takes", []):
            if param_spec["name"] not in params:
                raise ChannelValidationError(
                    f"Missing required parameter '{param_spec['name']}' for action '{action_display}'"
                )

        config = self.get_config(channel_name)
        if not config or not config.url:
            print(f"[Termin] Channel '{display}' action '{action_display}': no deploy config, invoke skipped")
            self._metrics[display]["sent"] += 1
            return {"ok": True, "status": "not_configured", "channel": display, "action": action_display}

        if config.protocol == "websocket" and display in self._ws_connections:
            ws_conn = self._ws_connections[display]
            if ws_conn.state != "connected":
                self._metrics[display]["errors"] += 1
                raise ChannelError(f"Channel '{display}': WebSocket not connected for action '{action_display}'")
            try:
                result = await ws_conn.invoke(action_snake, params)
                self._metrics[display]["sent"] += 1
                self._metrics[display]["last_active"] = _now_iso()
                return result
            except ChannelError:
                self._metrics[display]["errors"] += 1
                raise
        else:
            return await self._http_invoke(display, config, action_display, action_snake, params)

    async def _http_invoke(self, display: str, config: ChannelConfig,
                           action_display: str, action_snake: str, params: dict) -> dict:
        """Invoke an action over HTTP with retry."""
        headers = self._build_headers(config)
        url = f"{config.url.rstrip('/')}/actions/{action_snake}"
        last_error = None

        for attempt in range(config.max_retries + 1):
            try:
                response = await self._http_client.post(
                    url, json=params, headers=headers,
                    timeout=config.timeout_ms / 1000.0,
                )
                self._metrics[display]["sent"] += 1
                self._metrics[display]["last_active"] = _now_iso()

                if response.status_code < 400:
                    try:
                        result = response.json()
                    except (json.JSONDecodeError, ValueError):
                        result = {"raw": response.text}
                    return {"ok": True, "status": response.status_code, "result": result}
                else:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    if response.status_code < 500:
                        break
            except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as e:
                last_error = str(e)

            if attempt < config.max_retries:
                backoff = config.backoff_ms * (2 ** attempt) / 1000.0
                await asyncio.sleep(backoff)

        self._metrics[display]["errors"] += 1
        raise ChannelError(f"Action '{action_display}' on channel '{display}' failed: {last_error}")

    # ── Metrics ──

    def get_metrics(self, channel_name: str) -> dict:
        """Get send/receive/error metrics for a channel."""
        spec = self.get_spec(channel_name)
        if spec:
            display = spec["name"]["display"]
            return self._metrics.get(display, {})
        return {}

    def is_configured(self, channel_name: str) -> bool:
        """Check if a channel has deploy config (active provider, binding, or URL).

        Outbound/bidirectional channels need an explicit provider binding or URL.
        Inbound channels without an explicit binding are NOT considered configured
        (v0.9 decision — the operator must declare what sends to them).
        Internal channels are never configured (they're in-process buses).
        """
        spec = self.get_spec(channel_name)
        if spec:
            display = spec["name"]["display"]
            # Active provider (post-startup)
            if display in self._channel_providers:
                return True
            # Declared provider binding (pre-startup or no registry)
            binding = self._get_channel_binding(display)
            if binding and binding.get("provider"):
                return True
        # Legacy URL path (channels without provider_contract)
        config = self.get_config(channel_name)
        return config is not None and bool(config.url)

    def get_connection_state(self, channel_name: str) -> str:
        """Get live connection state for a channel."""
        spec = self.get_spec(channel_name)
        if not spec:
            return "not_configured"
        display = spec["name"]["display"]
        # v0.9 Phase 4: provider-based channels report "connected" once wired
        if display in self._channel_providers:
            return "connected"
        # Pre-startup: if the channel has a provider_contract and a binding
        # with a provider key, it's effectively "connected" (HTTP-style providers
        # are stateless — no persistent connection to check).
        contract = spec.get("provider_contract")
        if contract:
            binding = self._get_channel_binding(display)
            if binding and binding.get("provider"):
                return "connected"
            return "not_configured"
        # Legacy URL / WebSocket path (channels without provider_contract)
        config = self._channel_configs.get(display)
        if not config or not config.url:
            return "not_configured"
        if config.protocol == "websocket" and display in self._ws_connections:
            return self._ws_connections[display].state
        if config.protocol in ("http", "websocket"):
            return "connected" if config.protocol == "http" else "disconnected"
        return "disconnected"

    _CONTRACT_PROTOCOL: dict[str, str] = {
        "webhook":      "http",
        "email":        "email",
        "messaging":    "messaging",
        "event-stream": "event-stream",
    }

    def get_full_status(self) -> list[dict]:
        """Get full status of all channels for reflection."""
        result = []
        for ch in self._ir.get("channels", []):
            display = ch["name"]["display"]
            config = self._channel_configs.get(display)
            contract = ch.get("provider_contract")
            metrics = self._metrics.get(display, {})
            # v0.9: derive protocol from provider contract; fall back to URL config
            if contract:
                protocol = self._CONTRACT_PROTOCOL.get(contract, "provider")
            else:
                protocol = config.protocol if config else "none"
            entry = {
                "name": display,
                "direction": ch.get("direction", ""),
                "delivery": ch.get("delivery", ""),
                "carries": ch.get("carries_content", ""),
                "actions": len(ch.get("actions", [])),
                "protocol": protocol,
                "configured": self.is_configured(display),
                "state": self.get_connection_state(display),
                "metrics": {
                    "sent": metrics.get("sent", 0),
                    "received": metrics.get("received", 0),
                    "errors": metrics.get("errors", 0),
                    "last_active": metrics.get("last_active"),
                },
            }
            result.append(entry)
        return result



# ── Utilities ──

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
