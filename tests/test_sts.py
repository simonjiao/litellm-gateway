from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from sandbox_manager.sts import RustFSSTSClient, workspace_session_policy


@pytest.mark.asyncio
async def test_rustfs_sts_request_is_sigv4_signed_and_policy_scoped() -> None:
    observed: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers["authorization"]
        observed["content_sha256"] = request.headers["x-amz-content-sha256"]
        observed["body"] = request.content.decode()
        return httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <AssumeRoleResult><Credentials>
    <AccessKeyId>temporary-access</AccessKeyId>
    <SecretAccessKey>temporary-secret</SecretAccessKey>
    <SessionToken>temporary-token</SessionToken>
    <Expiration>2026-01-01T01:00:00Z</Expiration>
  </Credentials></AssumeRoleResult>
</AssumeRoleResponse>""",
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RustFSSTSClient(
        "http://rustfs:9000",
        "parent-access",
        "parent-secret",
        client=http,
    )
    policy = workspace_session_policy(
        "agent-workspaces", "repositories/workspace_test", writable=True
    )
    credentials = await client.assume_role(
        duration_seconds=900,
        policy=policy,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert credentials.access_key == "temporary-access"
    assert (
        "Credential=parent-access/20260101/us-east-1/s3/aws4_request" in observed["authorization"]
    )
    assert len(observed["content_sha256"]) == 64
    form = parse_qs(observed["body"])
    assert form["Action"] == ["AssumeRole"]
    assert form["DurationSeconds"] == ["900"]
    assert "repositories/workspace_test/*" in form["Policy"][0]
    await http.aclose()


def test_restore_policy_cannot_write_objects() -> None:
    policy = workspace_session_policy(
        "agent-workspaces", "repositories/workspace_test", writable=False
    )

    object_statement = policy["Statement"][1]
    assert object_statement["Action"] == ["s3:GetObject"]
