from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from typing import Any

import aiohttp

from .exceptions import APIAuthenticationError, APIConnectionError, APIError, APIRateLimitError
from .logging import done, loading
from .security import sign_hmac

log = logging.getLogger("quiddy.api")


class QuiddyAPIClient:
    """Authenticated client for api.quiddy.net.

    PostgreSQL and Redis stay behind the API. Every protected request is signed with
    timestamp + nonce + method + path + SHA256(body), matching the server verifier.
    """

    def __init__(self, http, *, base_url: str, client_id: str, secret: str, cfg: dict[str, Any]) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.secret = secret
        self.attempts = max(1, int(cfg.get("retry_attempts", 3)))
        self.base_delay = float(cfg.get("retry_base_delay_seconds", 0.35))
        self.max_delay = float(cfg.get("retry_max_delay_seconds", 3.0))
        self.request_timeout = float(cfg.get("request_timeout_seconds", 15.0))
        self._identity: dict[str, Any] | None = None

    async def start(self) -> None:
        loading(log, "Connecting to Quiddy Internal API…")
        self._identity = await self.get("/v1/me")
        done(log, "Authenticated to API as %s", self._identity.get("client", self.client_id))

    async def stop(self) -> None:
        self._identity = None

    async def health(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            data = await self.get("/health", auth=False, attempts=1)
            return {
                "status": "up" if data.get("status") == "ok" else "degraded",
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "endpoint": self.base_url,
                "client": self.client_id,
            }
        except Exception as exc:
            return {"status": "down", "endpoint": self.base_url, "error": f"{type(exc).__name__}: {exc}"}

    async def get(self, path: str, *, params: dict[str, Any] | None = None, auth: bool = True, attempts: int | None = None) -> Any:
        return await self.request("GET", path, params=params, auth=auth, attempts=attempts)

    async def post(self, path: str, *, json_data: Any | None = None, auth: bool = True, attempts: int | None = None) -> Any:
        return await self.request("POST", path, json_data=json_data, auth=auth, attempts=attempts)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: Any | None = None,
        auth: bool = True,
        attempts: int | None = None,
    ) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        if not self.http.session or self.http.session.closed:
            raise APIConnectionError("HTTP service is not running")

        body = b""
        headers = {"Accept": "application/json", "User-Agent": "QuiddyCore/0.2"}
        if json_data is not None:
            body = json.dumps(json_data, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
            headers["Content-Type"] = "application/json"

        max_attempts = max(1, attempts if attempts is not None else self.attempts)
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            request_headers = dict(headers)
            if auth:
                signed = sign_hmac(self.secret, method, path, body)
                request_headers.update({
                    "X-Quiddy-Client": self.client_id,
                    "X-Quiddy-Timestamp": str(signed.timestamp),
                    "X-Quiddy-Nonce": signed.nonce,
                    "X-Quiddy-Signature": signed.signature,
                    "X-Quiddy-Request-ID": uuid.uuid4().hex,
                })
            try:
                timeout = aiohttp.ClientTimeout(total=self.request_timeout)
                async with self.http.session.request(
                    method,
                    self.base_url + path,
                    params=params,
                    data=body if body else None,
                    headers=request_headers,
                    timeout=timeout,
                ) as response:
                    text = await response.text()
                    payload: Any
                    if text:
                        try:
                            payload = json.loads(text)
                        except json.JSONDecodeError:
                            payload = text
                    else:
                        payload = None
                    request_id = response.headers.get("X-Request-ID") or response.headers.get("CF-Ray")

                    if 200 <= response.status < 300:
                        return payload
                    detail = payload.get("detail") if isinstance(payload, dict) else payload
                    message = str(detail or f"API returned HTTP {response.status}")
                    if response.status in (401, 403):
                        raise APIAuthenticationError(message, status=response.status, request_id=request_id)
                    if response.status == 429:
                        retry_after = _retry_after(response.headers.get("Retry-After"))
                        last_error = APIRateLimitError(message, status=429, request_id=request_id, retry_after=retry_after)
                        if attempt + 1 < max_attempts:
                            await asyncio.sleep(retry_after or self._delay(attempt))
                            continue
                        raise last_error
                    if response.status >= 500:
                        last_error = APIError(message, status=response.status, request_id=request_id)
                        if attempt + 1 < max_attempts:
                            await asyncio.sleep(self._delay(attempt))
                            continue
                    raise APIError(message, status=response.status, request_id=request_id)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = APIConnectionError(f"{type(exc).__name__}: {exc}")
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(self._delay(attempt))
                    continue
                raise last_error from exc

        assert last_error is not None
        raise last_error

    def _delay(self, attempt: int) -> float:
        delay = min(self.max_delay, self.base_delay * (2**attempt))
        return max(0.0, delay * random.uniform(0.8, 1.2))


def _retry_after(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
