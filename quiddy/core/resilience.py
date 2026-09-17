from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeVar

T = TypeVar("T")


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.25,
    max_delay: float = 5.0,
    jitter: float = 0.2,
    retry_for: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except retry_for as exc:
            last = exc
            if attempt == attempts - 1:
                raise
            delay = min(max_delay, base_delay * (2**attempt))
            delay *= 1 + random.uniform(-jitter, jitter)
            await asyncio.sleep(max(delay, 0))
    assert last is not None
    raise last


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int = 5, reset_after: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self.failures = 0
        self.opened_at = 0.0
        self.state = CircuitState.CLOSED
        self._probe_lock = asyncio.Lock()

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        now = time.monotonic()
        if self.state == CircuitState.OPEN:
            if now - self.opened_at < self.reset_after:
                raise RuntimeError("circuit_open")
            self.state = CircuitState.HALF_OPEN

        if self.state == CircuitState.HALF_OPEN:
            if self._probe_lock.locked():
                raise RuntimeError("circuit_half_open")
            async with self._probe_lock:
                return await self._execute(operation)
        return await self._execute(operation)

    async def _execute(self, operation: Callable[[], Awaitable[T]]) -> T:
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()
            raise
        self.failures = 0
        self.state = CircuitState.CLOSED
        return result
