import hashlib
import hmac
import os
import time

from fastapi import HTTPException, Request
from redis.asyncio import Redis

CLIENT_ID = os.environ["QUIDDY_DISCORD_CLIENT_ID"]
CLIENT_SECRET = os.environ["QUIDDY_DISCORD_SECRET"].encode()
MAX_CLOCK_SKEW = 30
NONCE_TTL = 90
MAX_BODY_BYTES = 1_000_000


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def signature_payload(timestamp: str, nonce: str, method: str, path: str, body: bytes) -> bytes:
    return "\n".join((timestamp, nonce, method.upper(), path, body_hash(body))).encode()


async def authenticate(request: Request) -> str:
    client_id = request.headers.get("X-Quiddy-Client")
    timestamp = request.headers.get("X-Quiddy-Timestamp")
    nonce = request.headers.get("X-Quiddy-Nonce")
    signature = request.headers.get("X-Quiddy-Signature")

    if not all((client_id, timestamp, nonce, signature)):
        raise HTTPException(401, "Missing authentication headers")
    if client_id != CLIENT_ID:
        raise HTTPException(401, "Unknown client")
    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(401, "Invalid timestamp")
    if abs(int(time.time()) - ts) > MAX_CLOCK_SKEW:
        raise HTTPException(401, "Expired request")
    if len(nonce) < 16 or len(nonce) > 128:
        raise HTTPException(401, "Invalid nonce")

    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > MAX_BODY_BYTES:
                raise HTTPException(413, "Request too large")
        except ValueError:
            raise HTTPException(400, "Invalid content length")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(413, "Request too large")
    expected = hmac.new(
        CLIENT_SECRET,
        signature_payload(timestamp, nonce, request.method, request.url.path, body),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid signature")

    redis: Redis = request.app.state.redis
    accepted = await redis.set(f"auth:nonce:{client_id}:{nonce}", "1", ex=NONCE_TTL, nx=True)
    if not accepted:
        raise HTTPException(409, "Replay detected")
    return client_id
