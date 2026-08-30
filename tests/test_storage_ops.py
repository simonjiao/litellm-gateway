from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from storage_ops import cli
from storage_ops.cli import (
    StorageOperationError,
    capture,
    checkout,
    checkout_batch,
    prepare_workspace,
    publish,
    restore,
    retire_repository,
    upload_capture,
)


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


def test_prepare_workspace_controls_the_volume_root(tmp_path: Path) -> None:
    tmp_path.chmod(0o777)

    prepare_workspace(tmp_path)

    assert tmp_path.stat().st_mode & 0o777 == 0o755


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


def test_checkout_batch_is_atomically_visible_by_message(tmp_path: Path) -> None:
    contents = {"one": b"first", "two": b"second"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=contents[request.url.path.rsplit("/", 1)[-1]])

    artifacts = [
        {
            "artifact_id": f"artifact_{index:032x}",
            "filename": f"{name}.txt",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "max_bytes": len(content),
            "url": f"http://artifact/download/{name}",
            "token": f"token-{name}",
        }
        for index, (name, content) in enumerate(contents.items(), start=1)
    ]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    operation_id = "operation_" + "1" * 32

    result = checkout_batch(
        tmp_path,
        "user_message_1",
        "assistant_message_1",
        artifacts,
        operation_id,
        client=client,
    )

    committed = tmp_path / "uploads" / "user_message_1"
    assert result["already_committed"] is False
    assert (committed / "one.txt").read_bytes() == contents["one"]
    assert (committed / "two.txt").read_bytes() == contents["two"]
    assert committed.stat().st_mode & 0o777 == 0o555
    assert (committed / "one.txt").stat().st_mode & 0o777 == 0o444
    assert not list((tmp_path / ".agent-staging").iterdir())
    assert (tmp_path / "outputs" / "assistant_message_1").is_dir()

    repeated = checkout_batch(
        tmp_path,
        "user_message_1",
        "assistant_message_1",
        artifacts,
        operation_id,
        client=client,
    )
    assert repeated["already_committed"] is True
    client.close()


def test_checkout_batch_failure_exposes_no_partial_message_directory(tmp_path: Path) -> None:
    artifacts = [
        {
            "artifact_id": "artifact_" + "1" * 32,
            "filename": "good.txt",
            "size": 4,
            "sha256": hashlib.sha256(b"good").hexdigest(),
            "max_bytes": 4,
            "url": "http://artifact/good",
            "token": "one",
        },
        {
            "artifact_id": "artifact_" + "2" * 32,
            "filename": "bad.txt",
            "size": 3,
            "sha256": "0" * 64,
            "max_bytes": 3,
            "url": "http://artifact/bad",
            "token": "two",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"good" if request.url.path.endswith("good") else b"bad")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(StorageOperationError, match="digest"):
        checkout_batch(
            tmp_path,
            "user_message_2",
            "assistant_message_2",
            artifacts,
            "operation_" + "2" * 32,
            client=client,
        )
    assert not (tmp_path / "uploads" / "user_message_2").exists()
    assert not list((tmp_path / ".agent-staging").iterdir())
    client.close()


def test_empty_checkout_batch_prepares_the_current_output_directory(tmp_path: Path) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)))

    result = checkout_batch(
        tmp_path,
        "user_message_empty",
        "assistant_message_empty",
        [],
        "operation_" + "4" * 32,
        client=client,
    )

    assert result["artifacts"] == []
    assert (tmp_path / "uploads" / "user_message_empty").is_dir()
    assert (tmp_path / "outputs" / "assistant_message_empty").is_dir()
    client.close()


def test_publish_capture_survives_source_change_until_manifest_commit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    spool = tmp_path / "spool"
    output = workspace / "outputs" / "assistant_message_3"
    output.mkdir(parents=True)
    spool.mkdir()
    source = output / "report.csv"
    original = b"a,b\n1,2\n"
    source.write_bytes(original)
    operation_id = "operation_" + "3" * 32

    manifest = capture(
        workspace,
        spool,
        operation_id,
        "outputs/assistant_message_3/report.csv",
        max_bytes=1024,
    )
    source.write_bytes(b"changed after capture")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == original
        return httpx.Response(
            200,
            json={
                "artifact_id": "artifact_" + "3" * 32,
                "owner_id": "user_one",
                "filename": "report.csv",
                "media_type": "text/csv",
                "size": len(original),
                "sha256": hashlib.sha256(original).hexdigest(),
                "created_at": 100,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    descriptor = upload_capture(
        spool,
        operation_id,
        "http://artifact/upload",
        "one-shot-token",
        client=client,
    )
    assert manifest["sha256"] == descriptor["sha256"]
    assert not (spool / operation_id).exists()
    client.close()


def test_capture_rejects_a_symlinked_output_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    spool = tmp_path / "spool"
    outside = tmp_path / "outside"
    (workspace / "outputs").mkdir(parents=True)
    spool.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("not workspace output")
    (workspace / "outputs" / "assistant_message_link").symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(StorageOperationError, match="opened safely"):
        capture(
            workspace,
            spool,
            "operation_" + "5" * 32,
            "outputs/assistant_message_link/secret.txt",
            max_bytes=1024,
        )
