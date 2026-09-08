from __future__ import annotations

import hashlib
import os
import uuid

import httpx
import pytest
from pydantic import SecretStr

from artifact_service.app import create_app
from artifact_service.settings import ArtifactSettings
from artifact_service.store import S3ArtifactStore

pytestmark = pytest.mark.skipif(
    os.environ.get("ACI_RUSTFS_INTEGRATION") != "1",
    reason="set ACI_RUSTFS_INTEGRATION=1 for the real RustFS contract test",
)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required when ACI_RUSTFS_INTEGRATION=1")
    return value


@pytest.mark.asyncio
async def test_real_rustfs_upload_retry_inspect_download_and_delete() -> None:
    unique = uuid.uuid4().hex
    settings = ArtifactSettings(
        api_key=SecretStr("integration-service-key-" + "a" * 32),
        capability_secret=SecretStr("integration-capability-key-" + "b" * 32),
        s3_endpoint_url=_required("ACI_RUSTFS_ENDPOINT_URL"),
        s3_region=os.environ.get("ACI_RUSTFS_REGION", "us-east-1"),
        s3_access_key_id=_required("ACI_RUSTFS_ACCESS_KEY_ID"),
        s3_secret_access_key=SecretStr(_required("ACI_RUSTFS_SECRET_ACCESS_KEY")),
        s3_bucket=_required("ACI_RUSTFS_BUCKET"),
        s3_prefix=f"aci-contract-tests/{unique}",
        transfer_base_url="http://artifact.test",
        max_file_bytes=1024 * 1024,
    )
    store = S3ArtifactStore(settings)
    app = create_app(settings, store=store)
    service_headers = {
        "Authorization": f"Bearer {settings.api_key.get_secret_value()}"
    }
    content = b"real rustfs artifact contract\n"
    digest = hashlib.sha256(content).hexdigest()
    artifact_id: str | None = None

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://artifact.test",
        ) as client:
            ready = await client.get("/readyz")
            assert ready.status_code == 200, ready.text
            created = await client.post(
                "/v1/uploads",
                headers=service_headers,
                json={
                    "owner_id": f"integration:{unique}",
                    "filename": "contract.txt",
                    "media_type": "text/plain",
                    "max_bytes": len(content),
                    "expected_sha256": digest,
                    "subject_id": "integration-subject",
                    "app_id": "aci-contract-test",
                },
            )
            assert created.status_code == 201, created.text
            target = created.json()
            artifact_id = target["artifact_id"]
            capability_headers = {"Authorization": f"Bearer {target['token']}"}
            committed = await client.put(
                target["url"],
                headers=capability_headers,
                content=content,
            )
            assert committed.status_code == 200, committed.text
            retried = await client.put(
                target["url"],
                headers=capability_headers,
                content=content,
            )
            assert retried.status_code == 200, retried.text
            assert retried.json() == committed.json()
            inspected = await client.get(
                f"/v1/artifacts/{artifact_id}",
                headers=service_headers,
            )
            assert inspected.json() == committed.json()
            download_target = await client.post(
                f"/v1/artifacts/{artifact_id}/downloads",
                headers=service_headers,
                json={"subject_id": "integration-subject", "app_id": "aci-contract-test"},
            )
            downloaded = await client.get(
                download_target.json()["url"],
                headers={"Authorization": f"Bearer {download_target.json()['token']}"},
            )
            assert downloaded.content == content
            deleted = await client.delete(
                f"/v1/artifacts/{artifact_id}",
                headers=service_headers,
            )
            assert deleted.json() == {"artifact_id": artifact_id, "deleted": True}
            artifact_id = None

    assert artifact_id is None
