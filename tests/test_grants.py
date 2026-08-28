from __future__ import annotations

import pytest

from sandbox_api.grants import GrantError, issue_grant, verify_grant


def test_grant_round_trip_and_scope_validation() -> None:
    token = issue_grant(
        "operation-secret-at-least-32-bytes",
        issuer="open-webui-bff",
        audience="sandbox-manager",
        operation="sandbox_create",
        now=100,
        expires_in=60,
        workspace_id="workspace_abc",
    )

    claims = verify_grant(
        token,
        "operation-secret-at-least-32-bytes",
        audience="sandbox-manager",
        operation="sandbox_create",
        now=120,
    )

    assert claims["workspace_id"] == "workspace_abc"
    assert isinstance(claims["nonce"], str)


@pytest.mark.parametrize(
    ("secret", "audience", "operation", "now"),
    [
        ("wrong-secret-at-least-32-characters", "sandbox-manager", "sandbox_create", 120),
        ("operation-secret-at-least-32-bytes", "other", "sandbox_create", 120),
        ("operation-secret-at-least-32-bytes", "sandbox-manager", "publish", 120),
        ("operation-secret-at-least-32-bytes", "sandbox-manager", "sandbox_create", 160),
    ],
)
def test_grant_rejects_invalid_signature_scope_or_expiry(
    secret: str, audience: str, operation: str, now: int
) -> None:
    token = issue_grant(
        "operation-secret-at-least-32-bytes",
        issuer="open-webui-bff",
        audience="sandbox-manager",
        operation="sandbox_create",
        now=100,
        expires_in=60,
    )

    with pytest.raises(GrantError):
        verify_grant(
            token,
            secret,
            audience=audience,
            operation=operation,
            now=now,
        )
