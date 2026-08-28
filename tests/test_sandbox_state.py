from __future__ import annotations

import pytest

from sandbox_manager.state import StateConflictError, StateStore


def _store() -> StateStore:
    store = StateStore(":memory:")
    store.startup()
    return store


def test_workspace_claim_is_transactional_and_single_writer() -> None:
    store = _store()
    try:
        workspace = store.create_workspace(
            "workspace_test", kind="recoverable", volume_name="agent-workspace-test", now=10
        )
        assert workspace.status == "detached_clean"

        claimed, sandbox = store.claim_workspace(
            workspace.id,
            sandbox_id="sandbox_one",
            worker_host="sandbox-worker-one",
            container_name="sandbox-worker-one",
            created_at=20,
            expires_at=80,
        )
        assert claimed.active_sandbox_id == "sandbox_one"
        assert claimed.generation == 1
        assert sandbox.status == "starting"

        with pytest.raises(StateConflictError, match="already has a writer"):
            store.claim_workspace(
                workspace.id,
                sandbox_id="sandbox_two",
                worker_host="sandbox-worker-two",
                container_name="sandbox-worker-two",
                created_at=21,
                expires_at=81,
            )

        store.mark_sandbox_running("sandbox_one", now=22)
        finished = store.finish_sandbox(
            "sandbox_one", status="terminated", workspace_status="detached_dirty", now=30
        )
        assert finished.active_sandbox_id is None
        assert finished.status == "detached_dirty"
    finally:
        store.close()


def test_nonce_is_consumed_once_and_persists_until_expiry() -> None:
    store = _store()
    try:
        assert store.consume_nonce("nonce-one", 200, now=100) is True
        assert store.consume_nonce("nonce-one", 200, now=101) is False
        assert store.consume_nonce("nonce-one", 300, now=200) is True
    finally:
        store.close()


def test_operation_idempotency_reuses_existing_record() -> None:
    store = _store()
    try:
        store.create_workspace(
            "workspace_test", kind="recoverable", volume_name="agent-workspace-test", now=10
        )
        first, created = store.create_operation(
            "operation_one",
            operation="checkpoint",
            workspace_id="workspace_test",
            sandbox_id=None,
            idempotency_key="checkpoint:generation:1",
            input_data={"generation": 1},
            now=20,
        )
        second, second_created = store.create_operation(
            "operation_two",
            operation="checkpoint",
            workspace_id="workspace_test",
            sandbox_id=None,
            idempotency_key="checkpoint:generation:1",
            input_data={"generation": 1},
            now=21,
        )
        assert created is True
        assert second_created is False
        assert second.id == first.id
    finally:
        store.close()


def test_checkpoint_commit_atomically_advances_head_and_operation() -> None:
    store = _store()
    try:
        store.create_workspace(
            "workspace_test", kind="recoverable", volume_name="agent-workspace-test", now=10
        )
        store.claim_workspace(
            "workspace_test",
            sandbox_id="sandbox_one",
            worker_host="sandbox-worker-one",
            container_name="sandbox-worker-one",
            created_at=20,
            expires_at=80,
        )
        store.finish_sandbox(
            "sandbox_one", status="terminated", workspace_status="detached_dirty", now=30
        )
        operation, _ = store.create_operation(
            "operation_checkpoint",
            operation="checkpoint",
            workspace_id="workspace_test",
            sandbox_id=None,
            idempotency_key="checkpoint:workspace_test:1",
            input_data={"generation": 1},
            now=31,
        )
        store.transition_workspace(
            "workspace_test",
            expected={"detached_dirty"},
            status="checkpointing",
            now=32,
        )

        workspace, completed = store.commit_revision(
            "workspace_test",
            operation_id=operation.id,
            revision_id="snapshot-one",
            generation=1,
            result={"revision_id": "snapshot-one"},
            now=40,
        )

        assert workspace.status == "detached_clean"
        assert workspace.head_revision == "snapshot-one"
        assert completed.status == "succeeded"
        assert store.cleanup_candidates(39) == []
        assert store.cleanup_candidates(40) == [workspace]
    finally:
        store.close()
