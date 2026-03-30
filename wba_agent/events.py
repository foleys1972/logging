"""Simple async event bus for agent instrumentation and telemetry."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable, DefaultDict, Dict, List, Optional


@dataclass(frozen=True)
class Event:
    """Container for agent events and telemetry messages."""

    type: str
    payload: Dict[str, object]


EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Minimal asynchronous pub/sub mechanism."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[Event]" = asyncio.Queue()
        self._subscribers: DefaultDict[str, List[EventHandler]] = defaultdict(list)
        self._dispatcher_task: Optional[asyncio.Task[None]] = None

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register a coroutine callback for a given event type."""

        self._subscribers[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        """Enqueue an event for asynchronous processing."""

        await self._queue.put(event)

    def start(self) -> None:
        """Start the dispatcher task if not already running."""

        if self._dispatcher_task is None or self._dispatcher_task.done():
            loop = asyncio.get_event_loop()
            self._dispatcher_task = loop.create_task(self._dispatcher())

    async def stop(self) -> None:
        """Stop the dispatcher gracefully."""

        if self._dispatcher_task:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
            self._dispatcher_task = None

    async def _dispatcher(self) -> None:
        while True:
            event = await self._queue.get()
            handlers = self._subscribers.get(event.type, [])
            for handler in handlers:
                try:
                    await handler(event)
                except Exception:  # pragma: no cover - handler failures logged elsewhere
                    # In production this should log via structured logging.
                    pass


