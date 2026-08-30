#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import os
import re
import secrets
import shlex
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure the local deployment from an existing rclone S3 remote."
    )
    parser.add_argument("--remote", default="rustfs")
    parser.add_argument(
        "--rclone-config", type=Path, default=Path.home() / ".config/rclone/rclone.conf"
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()

    config = configparser.ConfigParser(interpolation=None)
    if not config.read(args.rclone_config) or args.remote not in config:
        parser.error(f"rclone remote '{args.remote}' was not found")
    remote = config[args.remote]
    endpoint = remote.get("endpoint", "").rstrip("/")
    access_key = remote.get("access_key_id", "")
    secret_key = remote.get("secret_access_key", "")
    if not endpoint.startswith(("http://", "https://")) or not access_key or not secret_key:
        parser.error("the rclone remote must contain endpoint and S3 access credentials")
    if not args.env_file.is_file():
        parser.error(f"environment file does not exist: {args.env_file}")

    existing = args.env_file.read_text().splitlines()
    current = _current_values(existing)
    operation_secret = current.get("SANDBOX_MANAGER_OPERATION_SIGNING_SECRET")
    if not operation_secret or operation_secret.startswith("replace-"):
        operation_secret = secrets.token_urlsafe(48)
    artifact_api_key = current.get("ARTIFACT_SERVICE_API_KEY")
    if not artifact_api_key or artifact_api_key.startswith("replace-"):
        artifact_api_key = secrets.token_urlsafe(48)
    artifact_capability_secret = current.get("ARTIFACT_SERVICE_CAPABILITY_SECRET")
    if not artifact_capability_secret or artifact_capability_secret.startswith("replace-"):
        artifact_capability_secret = secrets.token_urlsafe(48)
    updates = {
        "AGENT_OPEN_WEBUI_IMAGE": "agent-open-webui:0.3.0",
        "AGENT_STORAGE_OPS_IMAGE": "agent-storage-ops:0.3.0",
        "AGENT_ARTIFACT_SERVICE_IMAGE": "agent-artifact-service:0.3.0",
        "AGENT_WORKSPACE_ENABLED": "true",
        "CODEX_ADAPTER_AGENT_WORKSPACE": "/workspace/work",
        "OPEN_WEBUI_STORAGE_PROVIDER": "s3",
        "RUSTFS_ENDPOINT": endpoint,
        "RUSTFS_REGION": "us-east-1",
        "OPEN_WEBUI_S3_ACCESS_KEY_ID": access_key,
        "OPEN_WEBUI_S3_SECRET_ACCESS_KEY": secret_key,
        "OPEN_WEBUI_S3_BUCKET": "agent-data",
        "OPEN_WEBUI_S3_KEY_PREFIX": "open-webui/files",
        "ARTIFACT_SERVICE_API_KEY": artifact_api_key,
        "ARTIFACT_SERVICE_CAPABILITY_SECRET": artifact_capability_secret,
        "ARTIFACT_S3_ACCESS_KEY_ID": access_key,
        "ARTIFACT_S3_SECRET_ACCESS_KEY": secret_key,
        "ARTIFACT_S3_BUCKET": "agent-data",
        "ARTIFACT_S3_PREFIX": "artifacts",
        "WORKSPACE_S3_PARENT_ACCESS_KEY": access_key,
        "WORKSPACE_S3_PARENT_SECRET_KEY": secret_key,
        "WORKSPACE_S3_CREDENTIAL_MODE": "static",
        "WORKSPACE_S3_BUCKET": "agent-data",
        "WORKSPACE_S3_PREFIX": "workspaces",
        "SANDBOX_MANAGER_OPERATION_SIGNING_SECRET": operation_secret,
        "SANDBOX_MANAGER_STORAGE_ENABLED": "true",
        "SANDBOX_MANAGER_STORAGE_NETWORK": "agent-storage",
    }
    output = _updated_lines(existing, updates)
    mode = args.env_file.stat().st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{args.env_file.name}.", dir=args.env_file.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write("\n".join(output) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, args.env_file)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Configured {args.env_file} from rclone remote '{args.remote}' (credentials not shown).")
    return 0


def _current_values(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values


def _updated_lines(lines: list[str], updates: dict[str, str]) -> list[str]:
    remaining = dict(updates)
    result: list[str] = []
    for line in lines:
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            result.append(f"{key}={shlex.quote(remaining.pop(key))}")
        else:
            result.append(line)
    if remaining:
        if result and result[-1]:
            result.append("")
        result.append("# Workspace and RustFS integration.")
        result.extend(f"{key}={shlex.quote(value)}" for key, value in remaining.items())
    return result


if __name__ == "__main__":
    raise SystemExit(main())
