from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger("quiddy.events")
EventHandler = Callable[[Any], Awaitable[None] | None]


@dataclass(slots=True, frozen=True)
class Event:
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True, frozen=True)
class DiscordReady(Event):
    guild_count: int = 0
    user_count: int = 0


@dataclass(slots=True, frozen=True)
class PluginStateChanged(Event):
    plugin: str = ""
    old_state: str = ""
    new_state: str = ""


@dataclass(slots=True)
class _Subscription:
    token: int
    event_type: type
    handler: EventHandler
    priority: int
    owner: str | None


class EventBus:
    def __init__(self, *, handler_timeout: float = 15, max_concurrency: int = 64) -> None:
        self._subs: dict[type, list[_Subscription]] = defaultdict(list)
        self._next_token = 1
        self._timeout = handler_timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def subscribe(self, event_type: type, handler: EventHandler, *, priority: int = 0, owner: str | None = None) -> int:
        token = self._next_token
        self._next_token += 1
        self._subs[event_type].append(_Subscription(token, event_type, handler, priority, owner))
        self._subs[event_type].sort(key=lambda s: s.priority, reverse=True)
        return token

    def unsubscribe(self, token: int) -> None:
        for event_type, subs in list(self._subs.items()):
            self._subs[event_type] = [sub for sub in subs if sub.token != token]

    def unsubscribe_owner(self, owner: str) -> None:
        for event_type, subs in list(self._subs.items()):
            self._subs[event_type] = [sub for sub in subs if sub.owner != owner]

    async def publish(self, event: Any) -> None:
        handlers: list[_Subscription] = []
        for cls in type(event).mro():
            handlers.extend(self._subs.get(cls, ()))
        if not handlers:
            return
        await asyncio.gather(*(self._invoke(sub, event) for sub in handlers))

    async def _invoke(self, sub: _Subscription, event: Any) -> None:
        async with self._semaphore:
            try:
                result = sub.handler(event)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=self._timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Event handler failed event=%s owner=%s", type(event).__name__, sub.owner)
