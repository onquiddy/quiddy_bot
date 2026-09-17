from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from .exceptions import ServiceError
from .logging import done, loading

log = logging.getLogger("quiddy.services")
T = TypeVar("T")


class LifecycleService(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def health(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class ServiceSpec:
    name: str
    instance: Any
    dependencies: tuple[str, ...] = ()


class ServiceContainer:
    def __init__(self) -> None:
        self._services: dict[str, ServiceSpec] = {}
        self._start_order: list[str] = []

    def register(self, name: str, instance: Any, *, dependencies: tuple[str, ...] = ()) -> None:
        if name in self._services:
            raise ServiceError(f"Service already registered: {name}")
        self._services[name] = ServiceSpec(name, instance, dependencies)

    def get(self, name: str) -> Any:
        try:
            return self._services[name].instance
        except KeyError as exc:
            raise ServiceError(f"Unknown service: {name}") from exc

    def require(self, name: str, expected_type: type[T] | None = None) -> T:
        obj = self.get(name)
        if expected_type is not None and not isinstance(obj, expected_type):
            raise ServiceError(f"Service {name!r} is not {expected_type.__name__}")
        return obj

    def names(self) -> tuple[str, ...]:
        return tuple(self._services)

    def _topological_order(self) -> list[str]:
        graph: dict[str, set[str]] = {name: set(spec.dependencies) for name, spec in self._services.items()}
        for name, deps in graph.items():
            missing = deps - self._services.keys()
            if missing:
                raise ServiceError(f"Service {name} has missing dependencies: {sorted(missing)}")
        reverse: dict[str, set[str]] = defaultdict(set)
        indegree: dict[str, int] = {}
        for name, deps in graph.items():
            indegree[name] = len(deps)
            for dep in deps:
                reverse[dep].add(name)
        queue = deque(sorted(name for name, degree in indegree.items() if degree == 0))
        order: list[str] = []
        while queue:
            item = queue.popleft()
            order.append(item)
            for child in sorted(reverse[item]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(order) != len(graph):
            raise ServiceError("Service dependency cycle detected")
        return order

    async def start_all(self) -> None:
        started: list[str] = []
        try:
            for name in self._topological_order():
                spec = self._services[name]
                start = getattr(spec.instance, "start", None)
                if start:
                    loading(log, "Starting service %s…", name)
                    result = start()
                    if inspect.isawaitable(result):
                        await result
                started.append(name)
                done(log, "Service %s ready", name)
            self._start_order = started
        except Exception:
            log.exception("Service startup failed; rolling back")
            for name in reversed(started):
                try:
                    await self._stop_one(name)
                except Exception:
                    log.exception("Rollback failed for service %s", name)
            raise

    async def _stop_one(self, name: str) -> None:
        stop = getattr(self._services[name].instance, "stop", None)
        if stop:
            result = stop()
            if inspect.isawaitable(result):
                await result

    async def stop_all(self) -> None:
        for name in reversed(self._start_order):
            try:
                loading(log, "Останавливаю сервис %s…", name)
                await self._stop_one(name)
            except Exception:
                log.exception("Failed to stop service %s", name)
        self._start_order.clear()

    async def health_snapshot(self) -> dict[str, Any]:
        async def one(name: str, spec: ServiceSpec) -> tuple[str, Any]:
            health = getattr(spec.instance, "health", None)
            if not health:
                return name, {"status": "unknown"}
            try:
                result = health()
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=3)
                return name, result
            except Exception as exc:
                return name, {"status": "down", "error": f"{type(exc).__name__}: {exc}"}

        # Health endpoints are independent. Running them serially made /status wait for
        # the slowest service N times, which is pointless.
        pairs = await asyncio.gather(*(one(name, spec) for name, spec in self._services.items()))
        return dict(pairs)
