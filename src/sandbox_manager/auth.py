"""Sandbox Manager HTTP authentication."""

from __future__ import annotations

import secrets


def valid_bearer(authorization: str | None, expected: str) -> bool:
    if authorization is None or not authorization.startswith("Bearer "):
        return False
    supplied = authorization.removeprefix("Bearer ")
    return secrets.compare_digest(supplied.encode(), expected.encode())
