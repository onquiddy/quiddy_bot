from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .api import QuiddyAPIClient

log = logging.getLogger("quiddy.audit")


@dataclass(slots=True)
class AuditRecord:
    action: str
    source: str = "discord"
    guild_id: int | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None


class AuditService:
    def __init__(self, api: QuiddyAPIClient, cfg: dict[str, Any]) -> None:
        self.api = api
        self.enabled = bool(cfg.get("enabled", True))
        self.batch_size = int(cfg.get("batch_size", 100))
        self.flush_interval = float(cfg.get("flush_interval_seconds", 1))
        self.queue: asyncio.Queue[AuditRecord] = asyncio.Queue(maxsize=int(cfg.get("queue_size", 5000)))
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        if self.enabled:
            self._task = asyncio.create_task(self._worker(), name="audit-writer")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
            self._task = None

    async def write(self, record: AuditRecord) -> None:
        if not self.enabled:
            return
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            log.error("Audit queue overflow; flushing record immediately")
            await self._flush([record])

    async def _worker(self) -> None:
        while not self._stopping or not self.queue.empty():
            batch: list[AuditRecord] = []
            try:
                batch.append(await asyncio.wait_for(self.queue.get(), timeout=self.flush_interval))
            except TimeoutError:
                pass
            while len(batch) < self.batch_size and not self.queue.empty():
                batch.append(self.queue.get_nowait())
            if batch:
                try:
                    await self._flush(batch)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Failed to persist audit batch through API")

    async def _flush(self, records: list[AuditRecord]) -> None:
        payload = []
        for record in records:
            row = asdict(record)
            row["created_at"] = (row["created_at"] or datetime.now(timezone.utc)).isoformat()
            row["metadata"] = row["metadata"] or {}
            payload.append(row)
        await self.api.post("/v1/audit/batch", json_data={"records": payload})

    async def health(self) -> dict[str, Any]:
        return {"status": "up", "queued": self.queue.qsize(), "enabled": self.enabled}
