# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Outbound WebSocket channel connection with auto-reconnect.

Manages a single persistent outbound WebSocket connection with
automatic reconnection on disconnect using exponential backoff.
"""

import asyncio
import json
import logging
from typing import Optional, Callable, Coroutine

try:
    import websockets
    import websockets.asyncio.client
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

from .channel_config import ChannelConfig, ChannelError

logger = logging.getLogger("termin.channels")


class WebSocketConnection:
    """Manages a single persistent outbound WebSocket connection with auto-reconnect."""

    def __init__(self, channel_name: str, config: ChannelConfig,
                 on_message: Callable[[str, dict], Coroutine] = None):
        self.channel_name = channel_name
        self.config = config
        self.on_message = on_message
        self._ws = None
        self._reader_task: Optional[asyncio.Task] = None
        self._state = "disconnected"  # disconnected, connecting, connected, error
        self._reconnect_count = 0

    @property
    def state(self) -> str:
        return self._state

    async def connect(self):
        """Establish the WebSocket connection and start the reader loop."""
        if not HAS_WEBSOCKETS:
            self._state = "error"
            logger.error(f"Channel '{self.channel_name}': websockets package not installed")
            return

        self._state = "connecting"
        url = self.config.url

        # Build extra headers for auth
        extra_headers = {}
        auth = self.config.auth
        if auth.auth_type == "bearer" and auth.token:
            extra_headers["Authorization"] = f"Bearer {auth.token}"
        elif auth.auth_type == "api_key" and auth.token:
            extra_headers[auth.header] = auth.token

        try:
            self._ws = await websockets.asyncio.client.connect(
                url,
                additional_headers=extra_headers,
                ping_interval=self.config.heartbeat_ms / 1000.0 if self.config.heartbeat_ms else None,
            )
            self._state = "connected"
            self._reconnect_count = 0
            logger.info(f"Channel '{self.channel_name}': WebSocket connected to {url}")

            # Start reader loop
            self._reader_task = asyncio.create_task(self._reader_loop())
        except Exception as e:
            self._state = "error"
            logger.error(f"Channel '{self.channel_name}': WebSocket connect failed: {e}")
            if self.config.reconnect:
                asyncio.create_task(self._reconnect())

    async def _reader_loop(self):
        """Read messages from the WebSocket and dispatch to on_message callback."""
        try:
            async for raw_message in self._ws:
                try:
                    if isinstance(raw_message, bytes):
                        raw_message = raw_message.decode("utf-8")
                    data = json.loads(raw_message)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    data = {"raw": raw_message}

                if self.on_message:
                    try:
                        await self.on_message(self.channel_name, data)
                    except Exception as e:
                        logger.error(f"Channel '{self.channel_name}': message handler error: {e}")
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Channel '{self.channel_name}': WebSocket connection closed")
        except Exception as e:
            logger.error(f"Channel '{self.channel_name}': WebSocket reader error: {e}")
        finally:
            self._state = "disconnected"
            if self.config.reconnect:
                asyncio.create_task(self._reconnect())

    async def _reconnect(self):
        """Reconnect with exponential backoff, up to max_retries attempts."""
        self._reconnect_count += 1
        if self._reconnect_count > self.config.max_retries:
            logger.warning(f"Channel '{self.channel_name}': max reconnect attempts ({self.config.max_retries}) reached, giving up")
            self._state = "failed"
            return
        backoff = min(self.config.backoff_ms * (2 ** (self._reconnect_count - 1)) / 1000.0, 60.0)
        logger.info(f"Channel '{self.channel_name}': reconnecting in {backoff:.1f}s (attempt {self._reconnect_count}/{self.config.max_retries})")
        await asyncio.sleep(backoff)
        await self.connect()

    async def send(self, data: dict) -> bool:
        """Send a JSON message over the WebSocket."""
        if self._state != "connected" or not self._ws:
            raise ChannelError(f"Channel '{self.channel_name}': WebSocket not connected (state={self._state})")
        try:
            await self._ws.send(json.dumps(data))
            return True
        except Exception as e:
            raise ChannelError(f"Channel '{self.channel_name}': WebSocket send failed: {e}")

    async def invoke(self, action_name: str, params: dict) -> dict:
        """Invoke an action over WebSocket using request/response convention."""
        if self._state != "connected" or not self._ws:
            raise ChannelError(f"Channel '{self.channel_name}': WebSocket not connected (state={self._state})")
        import uuid
        msg_id = str(uuid.uuid4())[:8]
        try:
            await self._ws.send(json.dumps({
                "action": action_name,
                "params": params,
                "id": msg_id,
            }))
            return {"ok": True, "status": "sent", "id": msg_id}
        except Exception as e:
            raise ChannelError(f"Channel '{self.channel_name}': WebSocket invoke failed: {e}")

    async def close(self):
        """Close the WebSocket connection."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._state = "disconnected"
