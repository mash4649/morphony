from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from .types import Event, EventType

EventHandler = Callable[[Event], Awaitable[Any] | Any]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[EventType | None, list[EventHandler]] = {}

    def subscribe(
        self,
        event_type: EventType | None,
        handler: EventHandler,
    ) -> None:
        handlers = self._subscribers.setdefault(event_type, [])
        handlers.append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        self.subscribe(None, handler)

    async def publish(self, event: Event) -> None:
        handlers = [
            *self._subscribers.get(None, []),
            *self._subscribers.get(event.event_type, []),
        ]
        errors: list[Exception] = []

        for handler in handlers:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                errors.append(exc)

        if errors:
            raise ExceptionGroup(
                f"event handler failures for {event.event_type.value}",
                errors,
            )

    def publish_sync(self, event: Event) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.publish(event))
            return
        raise RuntimeError("publish_sync() cannot be called from a running event loop")


__all__ = ["EventBus", "EventHandler"]
