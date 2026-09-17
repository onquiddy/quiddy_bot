from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("quiddy.tasks")


class TaskSupervisor:
    """Owns background tasks so plugins cannot leak zombie coroutines on reload."""

    def __init__(self, name: str = "core") -> None:
        self.name = name
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing = False

    def create(self, coro: Awaitable[Any], *, name: str | None = None) -> asyncio.Task[Any]:
        if self._closing:
            raise RuntimeError(f"TaskSupervisor {self.name} is closing")
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            log.error("Background task %s failed", task.get_name(), exc_info=(type(exc), exc, exc.__traceback__))

    def interval(
        self,
        callback: Callable[[], Awaitable[Any]],
        seconds: float,
        *,
        name: str,
        jitter: float = 0.0,
        run_immediately: bool = False,
    ) -> asyncio.Task[Any]:
        async def runner() -> None:
            if run_immediately:
                await self._safe_call(callback, name)
            while True:
                delay = seconds + random.uniform(0, jitter) if jitter else seconds
                await asyncio.sleep(delay)
                await self._safe_call(callback, name)

        return self.create(runner(), name=f"{self.name}:{name}")

    async def _safe_call(self, callback: Callable[[], Awaitable[Any]], name: str) -> None:
        try:
            await callback()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Periodic task %s failed", name)

    async def close(self, timeout: float = 10) -> None:
        self._closing = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        except TimeoutError:
            log.warning("Timed out waiting for %d tasks in supervisor %s", len(tasks), self.name)
