from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from storage_ops.cli import StorageOperationError, checkout, publish


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
