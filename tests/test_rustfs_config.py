from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_rclone_rustfs_configuration_updates_env_without_printing_credentials(
    tmp_path: Path,
) -> None:
    rclone_config = tmp_path / "rclone.conf"
    rclone_config.write_text(
        "[rustfs]\n"
        "type = s3\n"
        "endpoint = http://rustfs.internal:9000\n"
        "access_key_id = test-access-key\n"
        "secret_access_key = test-secret-key\n"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LITELLM_MASTER_KEY=test\n"
        "SANDBOX_MANAGER_OPERATION_SIGNING_SECRET=existing-operation-secret-at-least-32-bytes\n"
    )

    result = subprocess.run(
        [
            str(ROOT / "scripts" / "configure-rustfs.py"),
            "--rclone-config",
            str(rclone_config),
            "--env-file",
            str(env_file),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "test-access-key" not in result.stdout
    assert "test-secret-key" not in result.stdout
    configured = env_file.read_text()
    assert "RUSTFS_ENDPOINT=http://rustfs.internal:9000" in configured
    assert "OPEN_WEBUI_S3_ACCESS_KEY_ID=test-access-key" in configured
    assert "WORKSPACE_S3_PARENT_ACCESS_KEY=test-access-key" in configured
    assert "WORKSPACE_S3_CREDENTIAL_MODE=static" in configured
    assert "SANDBOX_MANAGER_STORAGE_ENABLED=true" in configured
    assert "existing-operation-secret-at-least-32-bytes" in configured
    assert not list(tmp_path.glob("*.bak"))
