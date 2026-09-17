from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Policy = Callable[[Any], bool | Awaitable[bool]]


class PermissionEngine:
    def __init__(self, owner_ids: set[int]) -> None:
        self.owner_ids = owner_ids
        self._policies: dict[str, Policy] = {}

    def register(self, name: str, policy: Policy) -> None:
        if name in self._policies:
            raise ValueError(f"Policy already exists: {name}")
        self._policies[name] = policy

    def unregister(self, name: str) -> None:
        self._policies.pop(name, None)

    async def check(self, name: str, subject: Any) -> bool:
        if getattr(subject, "id", None) in self.owner_ids:
            return True
        policy = self._policies.get(name)
        if policy is None:
            return False
        result = policy(subject)
        if hasattr(result, "__await__"):
            result = await result
        return bool(result)
