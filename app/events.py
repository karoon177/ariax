# -*- coding: utf-8 -*-
"""
Tiny synchronous event bus decoupling the engine from transports.

The engine emits domain events (`trade`, `order`, `wallet`, ...) without
knowing whether they end up on a Bybit-style WebSocket topic, the legacy
UI channel, or the write-behind database queue. `app.main` wires the bus
to the WebSocket hub and the Persister at startup.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable


class EventBus:
    """In-process pub/sub; handlers must be non-blocking."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[dict], None]]] = {}

    def on(self, topic: str, handler: Callable[[dict], None]) -> None:
        self._handlers.setdefault(topic, []).append(handler)

    def emit(self, topic: str, payload: dict[str, Any]) -> None:
        for handler in self._handlers.get(topic, ()):
            try:
                handler(payload)
            except Exception:
                # Handlers are transport-level; never let one break trading.
                import logging
                logging.getLogger("ariax.events").exception("handler failed: %s", topic)


BUS = EventBus()
