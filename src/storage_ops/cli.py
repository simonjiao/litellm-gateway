from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import boto3  # pyright: ignore[reportMissingTypeStubs]
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
        elif args.operation == "retire":
            result = retire_repository(args.endpoint, args.bucket, args.prefix)
        elif args.operation == "checkout":
            result = checkout(
                Path(args.workspace),
                args.destination,
                args.url,
                args.token,
                max_bytes=args.max_bytes,
                expected_sha256=args.sha256,
            )
        elif args.operation == "checkout-batch":
            result = checkout_batch(
                Path(args.workspace),
                args.user_message_id,
                args.assistant_message_id,
                _json_list(args.artifacts),
                args.operation_id,
                agent_uid=args.agent_uid,
                agent_gid=args.agent_gid,
            )
        elif args.operation == "prepare":
            result = prepare_workspace(
                Path(args.workspace), agent_uid=args.agent_uid, agent_gid=args.agent_gid
            )
        elif args.operation == "capture":
            result = capture(
                Path(args.workspace),
                Path(args.spool),
                args.operation_id,
                args.source,
                max_bytes=args.max_bytes,
            )
        elif args.operation == "upload-capture":
            result = upload_capture(
                Path(args.spool),
                args.operation_id,
                args.url,
                args.token,
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
    if any(root.iterdir()):
        raise StorageOperationError("Restore target must be empty")
    _restic(["restore", revision, "--target", str(root)], cwd=root)
    return {"revision_id": revision}


def retire_repository(
    endpoint: str,
    bucket: str,
    prefix: str,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    parsed = urlsplit(endpoint.rstrip("/"))
    normalized = prefix.strip("/")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise StorageOperationError("Object-store endpoint is invalid")
    if not bucket or not normalized:
        raise StorageOperationError("Object-store bucket and prefix are required")

    own_client = client is None
    s3 = client or boto3.client(
        "s3",
        endpoint_url=endpoint.rstrip("/"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )
    deleted = 0
    try:
        while True:
            listing = s3.list_objects_v2(Bucket=bucket, Prefix=f"{normalized}/")
            contents = listing.get("Contents") or []
            objects = [
                {"Key": item["Key"]}
                for item in contents
                if isinstance(item, dict) and isinstance(item.get("Key"), str)
            ]
            if objects:
                response = s3.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": objects, "Quiet": True},
                )
                errors = response.get("Errors") or []
                if errors:
                    code = errors[0].get("Code") if isinstance(errors[0], dict) else None
                    raise StorageOperationError(
                        f"Object-store delete failed: {code or 'unknown error'}"
                    )
                deleted += len(objects)
            if not contents:
                break
    finally:
        if own_client:
            s3.close()
    return {"objects_deleted": deleted}


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


def prepare_workspace(
    workspace: Path,
    *,
    agent_uid: int = 10001,
    agent_gid: int = 10001,
) -> dict[str, Any]:
    root = workspace.resolve(strict=True)
    if not root.is_dir():
        raise StorageOperationError("Workspace is not a directory")
    work = _controlled_directory(root, "work", 0o770, agent_uid, agent_gid)
    _controlled_directory(root, "outputs", 0o755, 0, 0)
    _controlled_directory(root, "uploads", 0o555, 0, 0)
    _controlled_directory(root, ".agent-staging", 0o700, 0, 0)
    _fsync_directory(root)
    return {"work_path": str(work.relative_to(root))}


def checkout_batch(
    workspace: Path,
    user_message_id: str,
    assistant_message_id: str,
    artifacts: list[dict[str, Any]],
    operation_id: str,
    *,
    agent_uid: int = 10001,
    agent_gid: int = 10001,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    _message_id(user_message_id)
    _message_id(assistant_message_id)
    if len(artifacts) > 32:
        raise StorageOperationError("Checkout batch cannot contain more than 32 Artifacts")
    root = workspace.resolve(strict=True)
    prepare_workspace(root, agent_uid=agent_uid, agent_gid=agent_gid)
    uploads_root = root / "uploads"
    outputs_root = root / "outputs"
    final = uploads_root / user_message_id
    output = outputs_root / assistant_message_id
    public_manifest = _checkout_public_manifest(
        user_message_id, assistant_message_id, artifacts
    )
    if final.exists():
        _verify_checkout_commit(final, public_manifest)
        _controlled_directory(output, ".", 0o770, agent_uid, agent_gid)
        return {
            "path": f"uploads/{user_message_id}",
            "output_path": f"outputs/{assistant_message_id}",
            "artifacts": public_manifest["artifacts"],
            "already_committed": True,
        }

    staging_root = root / ".agent-staging"
    staging = staging_root / f"checkout-{_safe_operation_id(operation_id)}"
    if staging.exists():
        staging.chmod(0o700)
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700)
    own_client = client is None
    http = client or httpx.Client(timeout=None, trust_env=False)
    installed: list[dict[str, Any]] = []
    try:
        for item in public_manifest["artifacts"]:
            source = _artifact_source(artifacts, item["artifact_id"])
            target = staging / item["filename"]
            with target.open("xb") as stream:
                digest, size = _download(
                    http,
                    _required_text(source, "url"),
                    _required_text(source, "token"),
                    stream,
                    max_bytes=_positive_int(source, "max_bytes"),
                )
                stream.flush()
                os.fsync(stream.fileno())
            expected = source.get("sha256")
            if isinstance(expected, str) and digest != expected.lower():
                raise StorageOperationError("Downloaded file digest does not match")
            if size != int(item["size"]):
                raise StorageOperationError("Downloaded file size does not match")
            target.chmod(0o444)
            installed.append({**item, "sha256": digest, "size": size})
        committed_manifest = {**public_manifest, "artifacts": installed}
        manifest_path = staging / ".agent-checkout.json"
        _write_json_atomic(manifest_path, committed_manifest, mode=0o444)
        staging.chmod(0o755)
        _fsync_directory(staging)
        _controlled_directory(output, ".", 0o770, agent_uid, agent_gid)
        uploads_root.chmod(0o755)
        try:
            os.rename(staging, final)
            final.chmod(0o555)
            _fsync_directory(uploads_root)
        finally:
            uploads_root.chmod(0o555)
        return {
            "path": f"uploads/{user_message_id}",
            "output_path": f"outputs/{assistant_message_id}",
            "artifacts": installed,
            "already_committed": False,
        }
    except BaseException:
        if staging.exists():
            staging.chmod(0o700)
            shutil.rmtree(staging)
        raise
    finally:
        if own_client:
            http.close()


def capture(
    workspace: Path,
    spool: Path,
    operation_id: str,
    source: str,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    root = workspace.resolve(strict=True)
    spool_root = spool.resolve(strict=True)
    parts = Path(source).parts
    if (
        len(parts) < 3
        or parts[0] != "outputs"
        or not _valid_message_id(parts[1])
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise StorageOperationError("Published path is outside an assistant output directory")
    operation_dir = spool_root / _safe_operation_id(operation_id)
    manifest_path = operation_dir / "manifest.json"
    content_path = operation_dir / "content"
    if manifest_path.is_file() and content_path.is_file():
        existing = _read_json_object(manifest_path)
        if existing.get("source") != source:
            raise StorageOperationError("Capture operation is bound to another source")
        _verify_capture_content(content_path, existing)
        return existing
    operation_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
    temporary = operation_dir / ".content.part"
    descriptor = _open_workspace_file(root, parts)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise StorageOperationError("Published path must be a regular file")
        digest = hashlib.sha256()
        size = 0
        with temporary.open("xb") as output:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise StorageOperationError("Published file exceeds the operation limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after):
            raise StorageOperationError("Published file changed while it was captured")
    finally:
        os.close(descriptor)
    os.replace(temporary, content_path)
    result = {
        "source": source,
        "filename": parts[-1],
        "size": size,
        "sha256": digest.hexdigest(),
    }
    _write_json_atomic(manifest_path, result, mode=0o400)
    _fsync_directory(operation_dir)
    _fsync_directory(spool_root)
    return result


def upload_capture(
    spool: Path,
    operation_id: str,
    url: str,
    token: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    spool_root = spool.resolve(strict=True)
    operation_dir = spool_root / _safe_operation_id(operation_id)
    manifest = _read_json_object(operation_dir / "manifest.json")
    content = operation_dir / "content"
    _verify_capture_content(content, manifest)
    own_client = client is None
    http = client or httpx.Client(timeout=None, trust_env=False)
    try:
        with content.open("rb") as stream:
            response = http.put(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(manifest["size"]),
                },
                content=stream,
            )
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise StorageOperationError(f"Artifact upload failed: {exc}") from exc
    finally:
        if own_client:
            http.close()
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("artifact_id"), str)
        or result.get("sha256") != manifest["sha256"]
        or result.get("size") != manifest["size"]
    ):
        raise StorageOperationError("Artifact upload returned an invalid descriptor")
    shutil.rmtree(operation_dir)
    _fsync_directory(spool_root)
    return result


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


def _open_workspace_file(root: Path, parts: tuple[str, ...]) -> int:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    parent = os.open(root, directory_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(parts[-1], file_flags, dir_fd=parent)
    except OSError as exc:
        raise StorageOperationError("Published path cannot be opened safely") from exc
    finally:
        os.close(parent)


def _reject_symlink_parents(root: Path, parent: Path) -> None:
    current = parent
    while current != root:
        if current.is_symlink():
            raise StorageOperationError("Workspace path contains a symbolic link")
        current = current.parent
    if current != root:
        raise StorageOperationError("Workspace path escapes the mounted root")


def _controlled_directory(
    root: Path,
    relative: str,
    mode: int,
    uid: int,
    gid: int,
) -> Path:
    target = root if relative == "." else _safe_destination(root, relative)
    if target.exists():
        metadata = target.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise StorageOperationError(f"Controlled path is not a directory: {relative}")
    else:
        target.mkdir(parents=False, mode=mode)
    target.chmod(mode)
    if os.geteuid() == 0:
        os.chown(target, uid, gid)
    return target


def _checkout_public_manifest(
    user_message_id: str,
    assistant_message_id: str,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    names: set[str] = set()
    identifiers: set[str] = set()
    public: list[dict[str, Any]] = []
    for source in artifacts:
        artifact_id = _required_text(source, "artifact_id")
        filename = _safe_filename(_required_text(source, "filename"))
        size = _positive_int(source, "size", allow_zero=True)
        sha256 = _required_text(source, "sha256").lower()
        if not re.fullmatch(r"artifact_[a-f0-9]{32}", artifact_id):
            raise StorageOperationError("Checkout Artifact id is invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise StorageOperationError("Checkout Artifact digest is invalid")
        if artifact_id in identifiers or filename in names:
            raise StorageOperationError("Checkout Artifact ids and filenames must be unique")
        identifiers.add(artifact_id)
        names.add(filename)
        public.append(
            {
                "artifact_id": artifact_id,
                "filename": filename,
                "size": size,
                "sha256": sha256,
            }
        )
    return {
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "artifacts": public,
    }


def _verify_checkout_commit(final: Path, expected: dict[str, Any]) -> None:
    if final.is_symlink() or not final.is_dir():
        raise StorageOperationError("Checkout destination is not a controlled directory")
    manifest = _read_json_object(final / ".agent-checkout.json")
    if manifest != expected:
        raise StorageOperationError("Checkout destination is already bound to another batch")
    for item in expected["artifacts"]:
        target = final / item["filename"]
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise StorageOperationError("Committed checkout file is invalid")
        if metadata.st_size != item["size"]:
            raise StorageOperationError("Committed checkout file size changed")


def _artifact_source(artifacts: list[dict[str, Any]], artifact_id: str) -> dict[str, Any]:
    for source in artifacts:
        if source.get("artifact_id") == artifact_id:
            return source
    raise StorageOperationError("Checkout Artifact source is missing")


def _safe_filename(value: str) -> str:
    if (
        not value
        or len(value) > 255
        or value in {".", ".."}
        or Path(value).name != value
        or "\x00" in value
    ):
        raise StorageOperationError("Checkout filename is invalid")
    return value


def _required_text(value: dict[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise StorageOperationError(f"Operation {name} is invalid")
    return item


def _positive_int(value: dict[str, Any], name: str, *, allow_zero: bool = False) -> int:
    item = value.get(name)
    minimum = 0 if allow_zero else 1
    if not isinstance(item, int) or isinstance(item, bool) or item < minimum:
        raise StorageOperationError(f"Operation {name} is invalid")
    return item


def _valid_message_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


def _message_id(value: str) -> str:
    if not _valid_message_id(value):
        raise StorageOperationError("Message id is invalid")
    return value


def _safe_operation_id(value: str) -> str:
    if re.fullmatch(r"operation_[a-f0-9]{32}", value) is None:
        raise StorageOperationError("Operation id is invalid")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any], *, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.part")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageOperationError("Operation manifest is missing or invalid") from exc
    if not isinstance(value, dict):
        raise StorageOperationError("Operation manifest is invalid")
    return value


def _verify_capture_content(content: Path, manifest: dict[str, Any]) -> None:
    metadata = content.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StorageOperationError("Captured content is invalid")
    expected_size = manifest.get("size")
    expected_sha256 = manifest.get("sha256")
    if metadata.st_size != expected_size or not isinstance(expected_sha256, str):
        raise StorageOperationError("Captured content does not match its manifest")
    digest = hashlib.sha256()
    with content.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise StorageOperationError("Captured content digest changed")


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json_list(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise StorageOperationError("Operation Artifact list is invalid") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        raise StorageOperationError("Operation Artifact list is invalid")
    return parsed


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

    retire_parser = subparsers.add_parser("retire")
    retire_parser.add_argument("--endpoint", required=True)
    retire_parser.add_argument("--bucket", required=True)
    retire_parser.add_argument("--prefix", required=True)

    checkout_parser = subparsers.add_parser("checkout")
    checkout_parser.add_argument("--workspace", default="/workspace")
    checkout_parser.add_argument("--destination", required=True)
    checkout_parser.add_argument("--url", required=True)
    checkout_parser.add_argument("--token", required=True)
    checkout_parser.add_argument("--max-bytes", type=int, required=True)
    checkout_parser.add_argument("--sha256")

    checkout_batch_parser = subparsers.add_parser("checkout-batch")
    checkout_batch_parser.add_argument("--workspace", default="/workspace")
    checkout_batch_parser.add_argument("--operation-id", required=True)
    checkout_batch_parser.add_argument("--user-message-id", required=True)
    checkout_batch_parser.add_argument("--assistant-message-id", required=True)
    checkout_batch_parser.add_argument("--artifacts", required=True)
    checkout_batch_parser.add_argument("--agent-uid", type=int, default=10001)
    checkout_batch_parser.add_argument("--agent-gid", type=int, default=10001)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--workspace", default="/workspace")
    prepare_parser.add_argument("--agent-uid", type=int, default=10001)
    prepare_parser.add_argument("--agent-gid", type=int, default=10001)

    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--workspace", default="/workspace")
    capture_parser.add_argument("--spool", default="/spool")
    capture_parser.add_argument("--operation-id", required=True)
    capture_parser.add_argument("--source", required=True)
    capture_parser.add_argument("--max-bytes", type=int, required=True)

    upload_capture_parser = subparsers.add_parser("upload-capture")
    upload_capture_parser.add_argument("--spool", default="/spool")
    upload_capture_parser.add_argument("--operation-id", required=True)
    upload_capture_parser.add_argument("--url", required=True)
    upload_capture_parser.add_argument("--token", required=True)

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--workspace", default="/workspace")
    publish_parser.add_argument("--source", required=True)
    publish_parser.add_argument("--url", required=True)
    publish_parser.add_argument("--token", required=True)
    publish_parser.add_argument("--max-bytes", type=int, required=True)
    return parser
