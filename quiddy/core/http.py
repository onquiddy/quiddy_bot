from __future__ import annotations

import ssl

import aiohttp
import certifi


class HttpClient:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or {}
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(
            total=float(self.cfg.get("total_timeout_seconds", 20)),
            connect=float(self.cfg.get("connect_timeout_seconds", 5)),
            sock_read=float(self.cfg.get("read_timeout_seconds", 15)),
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(
            limit=int(self.cfg.get("connection_limit", 128)),
            limit_per_host=int(self.cfg.get("connection_limit_per_host", 32)),
            keepalive_timeout=float(self.cfg.get("keepalive_timeout_seconds", 30)),
            ttl_dns_cache=int(self.cfg.get("dns_cache_seconds", 300)),
            enable_cleanup_closed=True,
            ssl=ssl_context,
        )
        self.session = aiohttp.ClientSession(timeout=timeout, connector=connector, raise_for_status=False)

    async def stop(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def health(self) -> dict[str, str]:
        return {"status": "up" if self.session and not self.session.closed else "down"}
