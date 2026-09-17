from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path


class CommunityStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self.data = {"roles": {}}

    async def load(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                raw = json.loads(await asyncio.to_thread(self.path.read_text, "utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("roles", {}), dict):
                    self.data["roles"] = raw.get("roles", {})
            except (OSError, json.JSONDecodeError):
                broken = self.path.with_suffix(self.path.suffix + f".broken-{int(time.time())}")
                try:
                    os.replace(self.path, broken)
                except OSError:
                    pass
        return self

    async def _save(self) -> None:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        await asyncio.to_thread(self._write_atomic, payload)

    def _write_atomic(self, payload: str) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    async def set_roles(self, guild_id: int, user_id: int, roles: list[int]) -> None:
        async with self._lock:
            self.data.setdefault("roles", {}).setdefault(str(guild_id), {})[str(user_id)] = roles
            await self._save()

    def get_roles(self, guild_id: int, user_id: int) -> list[int]:
        return list(self.data.get("roles", {}).get(str(guild_id), {}).get(str(user_id), []))
