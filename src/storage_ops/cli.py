from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx


class StorageOperationError(RuntimeError):
    pass


class _BinaryWriter(Protocol):
    def write(self, data: bytes, /) -> int: ...


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.operation == "checkpoint":
            result = checkpoint(Path(args.workspace), args.workspace_id)
        elif args.operation == "restore":
            result = restore(args.revision, Path(args.target))
        elif args.operation == "checkout":
            result = checkout(
                Path(args.workspace),
                args.destination,
                args.url,
                args.token,
                max_bytes=args.max_bytes,
                expected_sha256=args.sha256,
            )
        elif args.operation == "publish":
            result = publish(
                Path(args.workspace),
                args.source,
                args.url,
                args.token,
                max_bytes=args.max_bytes,
            )
        else:  # pragma: no cover - argparse enforces the command
            parser.error("unknown operation")
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def checkpoint(workspace: Path, workspace_id: str) -> dict[str, Any]:
    root = workspace.resolve(strict=True)
    if not root.is_dir():
        raise StorageOperationError("Workspace is not a directory")
    probe = _restic(["snapshots", "--json"], cwd=root, check=False)
    if probe.returncode != 0:
        _restic(["init"], cwd=root)
    completed = _restic(
        ["backup", ".", "--json", "--tag", f"workspace:{workspace_id}"],
        cwd=root,
    )
    summary = _last_json_object(completed.stdout, message_type="summary")
    revision = summary.get("snapshot_id")
    if not isinstance(revision, str) or not revision:
        raise StorageOperationError("restic backup did not return snapshot_id")
    return {
        "revision_id": revision,
        "files_new": int(summary.get("files_new") or 0),
        "files_changed": int(summary.get("files_changed") or 0),
        "data_added": int(summary.get("data_added") or 0),
    }


def restore(revision: str, target: Path) -> dict[str, Any]:
    root = target.resolve(strict=True)
    if not root.is_dir():
        raise StorageOperationError("Restore target is not a directory")
    workspace_target = root / "workspace"
    if workspace_target.exists() and any(workspace_target.iterdir()):
        raise StorageOperationError("Restore target must be empty")
    _restic(["restore", revision, "--target", str(root)], cwd=root)
    if not workspace_target.is_dir():
        raise StorageOperationError("restic restore did not produce the Workspace root")
    return {"revision_id": revision}


def checkout(
    workspace: Path,
    destination: str,
    url: str,
    token: str,
    *,
    max_bytes: int,
    expected_sha256: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    target = _safe_destination(workspace, destination)
    _reject_symlink_parents(workspace.resolve(strict=True), target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_parents(workspace.resolve(strict=True), target.parent)
    own_client = client is None
    http = client or httpx.Client(timeout=None, trust_env=False)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".part",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            digest, size = _download(http, url, token, output, max_bytes=max_bytes)
            output.flush()
            os.fsync(output.fileno())
        if expected_sha256 is not None and digest != expected_sha256.lower():
            raise StorageOperationError("Downloaded file digest does not match")
        if target.is_symlink():
            raise StorageOperationError("Destination cannot be a symbolic link")
        os.replace(temporary, target)
        temporary = None
        return {"path": destination, "size": size, "sha256": digest}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if own_client:
            http.close()


def publish(
    workspace: Path,
    source: str,
    url: str,
    token: str,
    *,
    max_bytes: int,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    root = workspace.resolve(strict=True)
    path = _safe_existing_file(root, source)
    size = path.stat().st_size
    if size > max_bytes:
        raise StorageOperationError("Published file exceeds the operation limit")
    own_client = client is None
    http = client or httpx.Client(timeout=None, trust_env=False)
    try:
        with path.open("rb") as source_file:
            response = http.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (path.name, source_file, "application/octet-stream")},
            )
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise StorageOperationError(f"Artifact publish failed: {exc}") from exc
    finally:
        if own_client:
            http.close()
    if not isinstance(result, dict) or not isinstance(result.get("file_id"), str):
        raise StorageOperationError("Artifact publish returned an invalid result")
    return result


def _download(
    client: httpx.Client,
    url: str,
    token: str,
    output: _BinaryWriter,
    *,
    max_bytes: int,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with client.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise StorageOperationError("Downloaded file exceeds the operation limit")
                digest.update(chunk)
                output.write(chunk)
    except httpx.HTTPError as exc:
        raise StorageOperationError(f"Artifact checkout failed: {exc}") from exc
    return digest.hexdigest(), size


def _safe_destination(workspace: Path, relative: str) -> Path:
    root = workspace.resolve(strict=True)
    candidate = Path(relative)
    if candidate.is_absolute():
        raise StorageOperationError("Workspace path must be relative")
    if ".." in candidate.parts:
        raise StorageOperationError("Workspace path escapes the mounted root")
    target = root / candidate
    if target == root:
        raise StorageOperationError("Workspace path escapes the mounted root")
    return target


def _safe_existing_file(workspace: Path, relative: str) -> Path:
    path = _safe_destination(workspace, relative)
    _reject_symlink_parents(workspace, path.parent)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StorageOperationError("Published path must be a regular file")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(workspace):
        raise StorageOperationError("Published path escapes the mounted root")
    return path


def _reject_symlink_parents(root: Path, parent: Path) -> None:
    current = parent
    while current != root:
        if current.is_symlink():
            raise StorageOperationError("Workspace path contains a symbolic link")
        current = current.parent
    if current != root:
        raise StorageOperationError("Workspace path escapes the mounted root")


def _restic(
    arguments: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["restic", *arguments],
        cwd=cwd,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        message = completed.stderr.strip().splitlines()
        detail = message[-1] if message else "unknown error"
        raise StorageOperationError(f"restic failed: {detail}")
    return completed


def _last_json_object(output: str, *, message_type: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("message_type") == message_type:
            return value
    raise StorageOperationError(f"restic did not return a {message_type} result")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-storage-ops")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    checkpoint_parser = subparsers.add_parser("checkpoint")
    checkpoint_parser.add_argument("--workspace", default="/workspace")
    checkpoint_parser.add_argument("--workspace-id", required=True)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--revision", required=True)
    restore_parser.add_argument("--target", default="/restore")

    checkout_parser = subparsers.add_parser("checkout")
    checkout_parser.add_argument("--workspace", default="/workspace")
    checkout_parser.add_argument("--destination", required=True)
    checkout_parser.add_argument("--url", required=True)
    checkout_parser.add_argument("--token", required=True)
    checkout_parser.add_argument("--max-bytes", type=int, required=True)
    checkout_parser.add_argument("--sha256")

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--workspace", default="/workspace")
    publish_parser.add_argument("--source", required=True)
    publish_parser.add_argument("--url", required=True)
    publish_parser.add_argument("--token", required=True)
    publish_parser.add_argument("--max-bytes", type=int, required=True)
    return parser
