# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""EventBus pub/sub system for the Termin runtime.

Supports channel-based filtering: subscribers can listen to all events
(pattern=None) or only events matching a channel prefix pattern.

Channel ID convention:
  content.<name>.created   — new record
  content.<name>.updated   — record modified
  content.<name>.deleted   — record removed
  content.<name>           — matches all three (prefix match)
"""

import asyncio
from datetime import datetime


class EventBus:
    def __init__(self):
        self._subscribers: list[tuple[asyncio.Queue, str | None]] = []
        self._event_log: list[dict] = []

    def subscribe(self, channel_id: str | None = None) -> asyncio.Queue:
        """Subscribe to events. If channel_id is None, receive all events."""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append((q, channel_id))
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers = [(sq, pat) for sq, pat in self._subscribers if sq is not q]

    async def publish(self, event: dict):
        event.setdefault("log_level", "INFO")
        event.setdefault("timestamp", datetime.now().isoformat())
        self._event_log.append(event)
        if len(self._event_log) > 1000:
            self._event_log = self._event_log[-1000:]

        event_channel = event.get("channel_id")
        for q, pattern in self._subscribers:
            if pattern is None:
                # Unfiltered subscriber — receives everything
                await q.put(event)
            elif event_channel and event_channel.startswith(pattern):
                # Filtered subscriber — prefix match
                await q.put(event)

    def get_event_log(self):
        return list(self._event_log)
