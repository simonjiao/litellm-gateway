from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from storage_ops import cli
from storage_ops.cli import StorageOperationError, checkout, publish, restore, retire_repository


class _S3:
    def __init__(self) -> None:
        self.list_calls = 0
        self.deleted: list[str] = []

    def list_objects_v2(self, **_: Any) -> dict[str, Any]:
        self.list_calls += 1
        if self.list_calls == 1:
            return {"Contents": [{"Key": "workspaces/workspace_test/config"}]}
        if self.list_calls == 2:
            return {"Contents": [{"Key": "workspaces/workspace_test/data/one"}]}
        return {"Contents": []}

    def delete_objects(self, **request: Any) -> dict[str, Any]:
        self.deleted.extend(item["Key"] for item in request["Delete"]["Objects"])
        return {}


def test_retire_deletes_every_object_under_one_repository_prefix() -> None:
    s3 = _S3()

    result = retire_repository(
        "http://rustfs:9000",
        "agent-data",
        "workspaces/workspace_test",
        client=s3,
    )

    assert result == {"objects_deleted": 2}
    assert s3.deleted == [
        "workspaces/workspace_test/config",
        "workspaces/workspace_test/data/one",
    ]


def test_restore_writes_snapshot_contents_at_volume_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_restic(
        arguments: list[str], *, cwd: Path, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        assert arguments == ["restore", "snapshot-test", "--target", str(tmp_path)]
        assert cwd == tmp_path
        (cwd / "restore-proof.txt").write_text("restored")
        return subprocess.CompletedProcess(["restic"], 0, "", "")

    monkeypatch.setattr(cli, "_restic", fake_restic)

    assert restore("snapshot-test", tmp_path) == {"revision_id": "snapshot-test"}
    assert (tmp_path / "restore-proof.txt").read_text() == "restored"


def test_restore_rejects_a_nonempty_volume(tmp_path: Path) -> None:
    (tmp_path / "existing.txt").write_text("data")

    with pytest.raises(StorageOperationError, match="must be empty"):
        restore("snapshot-test", tmp_path)


def test_checkout_streams_and_atomically_installs_file(tmp_path: Path) -> None:
    content = b"workspace input"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer one-shot-token"
        return httpx.Response(200, content=content)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = checkout(
        tmp_path,
        "uploads/input.txt",
        "http://bff/transfer/file",
        "one-shot-token",
        max_bytes=1024,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        client=client,
    )

    assert (tmp_path / "uploads" / "input.txt").read_bytes() == content
    assert result["size"] == len(content)
    assert not list((tmp_path / "uploads").glob("*.part"))
    client.close()


def test_checkout_rejects_path_escape_and_symlink_parent(tmp_path: Path) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    with pytest.raises(StorageOperationError, match="escapes"):
        checkout(
            tmp_path,
            "../outside",
            "http://bff/file",
            "token",
            max_bytes=100,
            client=client,
        )

    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageOperationError, match="escapes|symbolic"):
        checkout(
            tmp_path,
            "linked/file",
            "http://bff/file",
            "token",
            max_bytes=100,
            client=client,
        )
    client.close()


def test_publish_allows_only_regular_files_inside_workspace(tmp_path: Path) -> None:
    source = tmp_path / "result.csv"
    source.write_text("a,b\n1,2\n")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer publish-token"
        assert b"result.csv" in request.content
        return httpx.Response(
            200,
            json={
                "file_id": "file_result",
                "download_url": "/api/v1/files/file_result/content?attachment=true",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = publish(
        tmp_path,
        "result.csv",
        "http://bff/transfer/publish",
        "publish-token",
        max_bytes=1024,
        client=client,
    )
    assert result["file_id"] == "file_result"

    (tmp_path / "link.csv").symlink_to(source)
    with pytest.raises(StorageOperationError, match="regular file"):
        publish(
            tmp_path,
            "link.csv",
            "http://bff/transfer/publish",
            "publish-token",
            max_bytes=1024,
            client=client,
        )
    client.close()
