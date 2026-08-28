from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any


class GrantError(ValueError):
    pass


def issue_grant(
    secret: str,
    *,
    issuer: str,
    audience: str,
    operation: str,
    expires_in: int = 120,
    now: int | None = None,
    nonce: str | None = None,
    **claims: Any,
) -> str:
    issued_at = int(time.time()) if now is None else now
    payload = {
        "iss": issuer,
        "aud": audience,
        "operation": operation,
        "iat": issued_at,
        "exp": issued_at + expires_in,
        "nonce": nonce or uuid.uuid4().hex,
        **claims,
    }
    encoded = _encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_encode(signature)}"


def verify_grant(
    token: str,
    secret: str,
    *,
    audience: str,
    operation: str | None = None,
    now: int | None = None,
    max_lifetime: int = 900,
) -> dict[str, Any]:
    if not token or len(token) > 64 * 1024:
        raise GrantError("invalid grant")
    try:
        encoded, supplied_signature = token.split(".", 1)
        signature = _decode(supplied_signature)
    except (ValueError, UnicodeError) as exc:
        raise GrantError("invalid grant") from exc
    expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise GrantError("invalid grant signature")
    try:
        payload = json.loads(_decode(encoded))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise GrantError("invalid grant payload") from exc
    if not isinstance(payload, dict):
        raise GrantError("invalid grant payload")

    current = int(time.time()) if now is None else now
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    nonce = payload.get("nonce")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        raise GrantError("grant timestamps are required")
    if issued_at > current + 30 or expires_at <= current:
        raise GrantError("grant is expired or not active")
    if expires_at - issued_at <= 0 or expires_at - issued_at > max_lifetime:
        raise GrantError("grant lifetime is invalid")
    if payload.get("aud") != audience:
        raise GrantError("grant audience does not match")
    if operation is not None and payload.get("operation") != operation:
        raise GrantError("grant operation does not match")
    if not isinstance(payload.get("iss"), str) or not isinstance(nonce, str) or not nonce:
        raise GrantError("grant identity is invalid")
    return payload


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
