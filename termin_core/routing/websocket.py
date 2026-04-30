# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""WebSocket Protocol — the framework-agnostic surface that
WebSocket route handlers operate on.

Per Q4 of the Phase 7 routing briefing: termin-core owns the
*minimal* Protocol (bytes-on-the-wire only). Topic dispatch +
per-connection subscription registry + fanout lives in
:mod:`termin_core.routing.channel_dispatch` (added in slice 7.2.f).
Adapters supply the concrete WebSocket implementation; everything
above the wire is core.

The Protocol is intentionally narrow — six methods plus a
``principal`` attribute. Adapters that wrap a framework's WS type
(``fastapi.WebSocket``, ``starlette.WebSocket``, etc.) need to
satisfy this interface; that's the entire bridge.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, TYPE_CHECKING, runtime_checkable

if TYPE_CHECKING:
    from ..providers.identity_contract import Principal


@runtime_checkable
class TerminWebSocket(Protocol):
    """The bytes-on-the-wire WebSocket interface.

    Adapters supply an implementation that wraps their framework's
    native WebSocket. The runtime's WS dispatcher (slice 7.2.f
    deliverable) operates on instances of this type without ever
    touching the framework directly.

    Attributes:
        principal: The authenticated caller for this connection.
            Adapter middleware extracts on connect (same pattern as
            HTTP, per Q3=a of the routing briefing) and assigns
            before handing the socket to the runtime. May be None
            when no identity provider is bound; handlers should
            treat that as the anonymous case.

    Methods are async because every reasonable WebSocket
    implementation is async; sync hosts can wrap a thread with
    asyncio.run_coroutine_threadsafe in their adapter if needed.
    """

    principal: Optional["Principal"]

    async def accept(self) -> None:
        """Accept the incoming connection. Adapter handles any
        subprotocol / extension negotiation before this returns."""
        ...

    async def send_json(self, data: Any) -> None:
        """Serialize ``data`` as JSON and send as a text frame."""
        ...

    async def send_bytes(self, data: bytes) -> None:
        """Send raw bytes as a binary frame."""
        ...

    async def receive_json(self) -> Any:
        """Receive the next text frame and parse as JSON. Raises
        if the frame isn't valid JSON or the connection closes."""
        ...

    async def receive_text(self) -> str:
        """Receive the next text frame as-is."""
        ...

    async def close(self, code: int = 1000) -> None:
        """Close the connection with the given WebSocket close code.
        1000 is normal closure; 1011 is internal error; 4xxx codes
        are app-defined."""
        ...


__all__ = ["TerminWebSocket"]
