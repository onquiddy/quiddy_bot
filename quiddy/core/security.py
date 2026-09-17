from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SignedRequest:
    timestamp: int
    nonce: str
    signature: str


def sign_hmac(secret: str, method: str, path: str, body: bytes, *, timestamp: int | None = None, nonce: str | None = None) -> SignedRequest:
    ts = int(timestamp or time.time())
    nonce = nonce or secrets.token_urlsafe(18)
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{ts}\n{nonce}\n{method.upper()}\n{path}\n{body_hash}".encode()
    signature = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    return SignedRequest(ts, nonce, signature)


def verify_hmac(secret: str, signed: SignedRequest, method: str, path: str, body: bytes, *, max_skew_seconds: int = 60) -> bool:
    if abs(int(time.time()) - signed.timestamp) > max_skew_seconds:
        return False
    expected = sign_hmac(secret, method, path, body, timestamp=signed.timestamp, nonce=signed.nonce)
    return hmac.compare_digest(expected.signature, signed.signature)
