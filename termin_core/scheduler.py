# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Termin Scheduler — periodic Compute execution via asyncio.

Computes with `Trigger on schedule every X` auto-execute on a timer.
Parses trigger specs like:
  "schedule every 1 hour"
  "schedule every 5 minutes"
  "schedule every 30 seconds"

Each scheduled execution calls the same _execute_compute() used by
event triggers, keeping a consistent execution path.
"""

import asyncio
import re
from datetime import datetime, timezone


# Pattern: "schedule every <N> <unit>"
_SCHEDULE_RE = re.compile(
    r"^schedule\s+every\s+(\d+)\s+(second|seconds|minute|minutes|hour|hours|day|days)$",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "second": 1, "seconds": 1,
    "minute": 60, "minutes": 60,
    "hour": 3600, "hours": 3600,
    "day": 86400, "days": 86400,
}


def parse_schedule_interval(trigger: str) -> float | None:
    """Parse a trigger string and return the interval in seconds, or None if not a schedule trigger."""
    if not trigger:
        return None
    m = _SCHEDULE_RE.match(trigger.strip())
    if not m:
        return None
    count = int(m.group(1))
    unit = m.group(2).lower()
    return count * _UNIT_SECONDS[unit]


class Scheduler:
    """Manages periodic Compute tasks via asyncio.

    Usage:
        scheduler = Scheduler()
        scheduler.register(compute_spec, execute_fn)
        await scheduler.start()
        ...
        await scheduler.stop()

    The execute_fn should be an async callable that takes (comp_dict, record, content_name).
    """

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._registered: list[tuple[dict, float]] = []
        self._execute_fn = None

    def register(self, comp: dict, interval_seconds: float, execute_fn):
        """Register a Compute for periodic execution."""
        self._registered.append((comp, interval_seconds))
        self._execute_fn = execute_fn

    async def start(self):
        """Start all registered scheduled tasks."""
        for comp, interval in self._registered:
            task = asyncio.create_task(self._run_loop(comp, interval))
            self._tasks.append(task)
            print(f"[Termin] Scheduler: '{comp['name']['display']}' every {interval}s")

    async def stop(self):
        """Cancel all scheduled tasks."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        if self._registered:
            print(f"[Termin] Scheduler: stopped {len(self._registered)} task(s)")

    async def _run_loop(self, comp: dict, interval: float):
        """Periodically execute a Compute."""
        comp_name = comp["name"]["display"]
        try:
            while True:
                await asyncio.sleep(interval)
                ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                print(f"[Termin] Scheduler: executing '{comp_name}' at {ts}")
                try:
                    await self._execute_fn(comp, {}, "")
                except Exception as e:
                    print(f"[Termin] Scheduler: '{comp_name}' failed: {e}")
        except asyncio.CancelledError:
            pass

    @property
    def task_count(self) -> int:
        """Number of registered scheduled tasks."""
        return len(self._registered)
