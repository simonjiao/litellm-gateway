from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class StateNotFoundError(LookupError):
    pass


class StateConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    id: str
    kind: str
    status: str
    volume_name: str
    generation: int
    head_revision: str | None
    active_sandbox_id: str | None
    created_at: int
    updated_at: int
    delete_after: int | None


@dataclass(frozen=True, slots=True)
class SandboxRecord:
    id: str
    workspace_id: str
    worker_host: str
    container_name: str
    status: str
    created_at: int
    expires_at: int | None


@dataclass(frozen=True, slots=True)
class OperationRecord:
    id: str
    operation: str
    status: str
    workspace_id: str
    sandbox_id: str | None
    idempotency_key: str
    input: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    created_at: int
    updated_at: int


class StateStore:
    """Single-process SQLite state store for Manager control state."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def startup(self) -> None:
        if self._connection is not None:
            return
        if self._path != ":memory:":
            Path(self._path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._path,
            isolation_level=None,
            check_same_thread=False,
            timeout=30,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('ephemeral', 'recoverable')),
                status TEXT NOT NULL,
                volume_name TEXT NOT NULL UNIQUE,
                generation INTEGER NOT NULL DEFAULT 0,
                head_revision TEXT,
                active_sandbox_id TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                delete_after INTEGER
            );

            CREATE TABLE IF NOT EXISTS sandboxes (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                worker_host TEXT NOT NULL,
                container_name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS sandboxes_workspace_idx
                ON sandboxes(workspace_id, status);

            CREATE TABLE IF NOT EXISTS revisions (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                snapshot_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(workspace_id, snapshot_id)
            );

            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                status TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                sandbox_id TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                input_json TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS operations_workspace_idx
                ON operations(workspace_id, status);

            CREATE TABLE IF NOT EXISTS consumed_nonces (
                nonce TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER NOT NULL
            );
            """
        )
        self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        connection = self._require_connection()
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def create_workspace(
        self,
        workspace_id: str,
        *,
        kind: str,
        volume_name: str,
        now: int | None = None,
    ) -> WorkspaceRecord:
        timestamp = int(time.time()) if now is None else now
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, kind, status, volume_name, created_at, updated_at
                ) VALUES (?, ?, 'detached_clean', ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (workspace_id, kind, volume_name, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            assert row is not None
            if row["kind"] != kind or row["volume_name"] != volume_name:
                raise StateConflictError(f"Workspace '{workspace_id}' already exists")
            return _workspace(row)

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        with self._lock:
            row = (
                self._require_connection()
                .execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
                .fetchone()
            )
        if row is None:
            raise StateNotFoundError(f"Workspace '{workspace_id}' was not found")
        return _workspace(row)

    def claim_workspace(
        self,
        workspace_id: str,
        *,
        sandbox_id: str,
        worker_host: str,
        container_name: str,
        created_at: int,
        expires_at: int,
    ) -> tuple[WorkspaceRecord, SandboxRecord]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            if row is None:
                raise StateNotFoundError(f"Workspace '{workspace_id}' was not found")
            if row["active_sandbox_id"] not in (None, sandbox_id):
                raise StateConflictError(f"Workspace '{workspace_id}' already has a writer")
            if row["status"] in {"checkpointing", "restoring", "deleting", "deleted"}:
                raise StateConflictError(
                    f"Workspace '{workspace_id}' is not mountable while {row['status']}"
                )
            connection.execute(
                """
                INSERT INTO sandboxes (
                    id, workspace_id, worker_host, container_name, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, 'starting', ?, ?)
                """,
                (
                    sandbox_id,
                    workspace_id,
                    worker_host,
                    container_name,
                    created_at,
                    expires_at,
                ),
            )
            connection.execute(
                """
                UPDATE workspaces
                SET status = 'running', active_sandbox_id = ?, generation = generation + 1,
                    updated_at = ?, delete_after = NULL
                WHERE id = ?
                """,
                (sandbox_id, created_at, workspace_id),
            )
            workspace_row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            sandbox_row = connection.execute(
                "SELECT * FROM sandboxes WHERE id = ?", (sandbox_id,)
            ).fetchone()
            assert workspace_row is not None and sandbox_row is not None
            return _workspace(workspace_row), _sandbox(sandbox_row)

    def mark_sandbox_running(self, sandbox_id: str, *, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE sandboxes SET status = 'running' WHERE id = ? AND status = 'starting'",
                (sandbox_id,),
            ).rowcount
            if changed != 1:
                raise StateConflictError(f"Sandbox '{sandbox_id}' is not starting")
            connection.execute(
                """
                UPDATE workspaces SET updated_at = ?
                WHERE active_sandbox_id = ?
                """,
                (timestamp, sandbox_id),
            )

    def get_sandbox(self, sandbox_id: str) -> SandboxRecord:
        with self._lock:
            row = (
                self._require_connection()
                .execute("SELECT * FROM sandboxes WHERE id = ?", (sandbox_id,))
                .fetchone()
            )
        if row is None:
            raise StateNotFoundError(f"Sandbox '{sandbox_id}' was not found")
        return _sandbox(row)

    def active_sandboxes(self) -> list[SandboxRecord]:
        with self._lock:
            rows = (
                self._require_connection()
                .execute("SELECT * FROM sandboxes WHERE status IN ('starting', 'running')")
                .fetchall()
            )
        return [_sandbox(row) for row in rows]

    def renew_sandbox(self, sandbox_id: str, expires_at: int) -> SandboxRecord:
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE sandboxes SET expires_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (expires_at, sandbox_id),
            ).rowcount
            if changed != 1:
                raise StateConflictError(f"Sandbox '{sandbox_id}' is not running")
            row = connection.execute(
                "SELECT * FROM sandboxes WHERE id = ?", (sandbox_id,)
            ).fetchone()
            assert row is not None
            return _sandbox(row)

    def finish_sandbox(
        self,
        sandbox_id: str,
        *,
        status: str,
        workspace_status: str,
        now: int | None = None,
    ) -> WorkspaceRecord:
        timestamp = int(time.time()) if now is None else now
        with self.transaction() as connection:
            sandbox = connection.execute(
                "SELECT * FROM sandboxes WHERE id = ?", (sandbox_id,)
            ).fetchone()
            if sandbox is None:
                raise StateNotFoundError(f"Sandbox '{sandbox_id}' was not found")
            connection.execute(
                "UPDATE sandboxes SET status = ?, expires_at = NULL WHERE id = ?",
                (status, sandbox_id),
            )
            connection.execute(
                """
                UPDATE workspaces
                SET status = ?, active_sandbox_id = NULL, updated_at = ?
                WHERE id = ? AND active_sandbox_id = ?
                """,
                (workspace_status, timestamp, sandbox["workspace_id"], sandbox_id),
            )
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?", (sandbox["workspace_id"],)
            ).fetchone()
            assert row is not None
            return _workspace(row)

    def delete_workspace(self, workspace_id: str) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT active_sandbox_id FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            if row is None:
                return
            if row["active_sandbox_id"] is not None:
                raise StateConflictError(f"Workspace '{workspace_id}' still has a writer")
            connection.execute("DELETE FROM operations WHERE workspace_id = ?", (workspace_id,))
            connection.execute("DELETE FROM revisions WHERE workspace_id = ?", (workspace_id,))
            connection.execute("DELETE FROM sandboxes WHERE workspace_id = ?", (workspace_id,))
            connection.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))

    def schedule_workspace_delete(
        self, workspace_id: str, delete_after: int, *, now: int | None = None
    ) -> WorkspaceRecord:
        timestamp = int(time.time()) if now is None else now
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE workspaces SET delete_after = ?, updated_at = ?
                WHERE id = ? AND kind = 'recoverable'
                """,
                (delete_after, timestamp, workspace_id),
            ).rowcount
            if changed != 1:
                raise StateNotFoundError(f"Recoverable Workspace '{workspace_id}' was not found")
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            assert row is not None
            return _workspace(row)

    def consume_nonce(self, nonce: str, expires_at: int, *, now: int | None = None) -> bool:
        timestamp = int(time.time()) if now is None else now
        with self.transaction() as connection:
            connection.execute("DELETE FROM consumed_nonces WHERE expires_at <= ?", (timestamp,))
            try:
                connection.execute(
                    "INSERT INTO consumed_nonces (nonce, expires_at, consumed_at) VALUES (?, ?, ?)",
                    (nonce, expires_at, timestamp),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def create_operation(
        self,
        operation_id: str,
        *,
        operation: str,
        workspace_id: str,
        sandbox_id: str | None,
        idempotency_key: str,
        input_data: dict[str, Any],
        now: int | None = None,
    ) -> tuple[OperationRecord, bool]:
        timestamp = int(time.time()) if now is None else now
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM operations WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                return _operation(existing), False
            connection.execute(
                """
                INSERT INTO operations (
                    id, operation, status, workspace_id, sandbox_id, idempotency_key,
                    input_json, created_at, updated_at
                ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    operation,
                    workspace_id,
                    sandbox_id,
                    idempotency_key,
                    json.dumps(input_data, sort_keys=True, separators=(",", ":")),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            assert row is not None
            return _operation(row), True

    def get_operation(self, operation_id: str) -> OperationRecord:
        with self._lock:
            row = (
                self._require_connection()
                .execute("SELECT * FROM operations WHERE id = ?", (operation_id,))
                .fetchone()
            )
        if row is None:
            raise StateNotFoundError(f"Operation '{operation_id}' was not found")
        return _operation(row)

    def update_operation(
        self,
        operation_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        now: int | None = None,
    ) -> OperationRecord:
        timestamp = int(time.time()) if now is None else now
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE operations
                SET status = ?, result_json = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(result, sort_keys=True, separators=(",", ":"))
                    if result is not None
                    else None,
                    error,
                    timestamp,
                    operation_id,
                ),
            ).rowcount
            if changed != 1:
                raise StateNotFoundError(f"Operation '{operation_id}' was not found")
            row = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            assert row is not None
            return _operation(row)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("State store is not started")
        return self._connection


def _workspace(row: sqlite3.Row) -> WorkspaceRecord:
    return WorkspaceRecord(
        id=str(row["id"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        volume_name=str(row["volume_name"]),
        generation=int(row["generation"]),
        head_revision=str(row["head_revision"]) if row["head_revision"] is not None else None,
        active_sandbox_id=(
            str(row["active_sandbox_id"]) if row["active_sandbox_id"] is not None else None
        ),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        delete_after=int(row["delete_after"]) if row["delete_after"] is not None else None,
    )


def _sandbox(row: sqlite3.Row) -> SandboxRecord:
    return SandboxRecord(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        worker_host=str(row["worker_host"]),
        container_name=str(row["container_name"]),
        status=str(row["status"]),
        created_at=int(row["created_at"]),
        expires_at=int(row["expires_at"]) if row["expires_at"] is not None else None,
    )


def _operation(row: sqlite3.Row) -> OperationRecord:
    result = json.loads(row["result_json"]) if row["result_json"] is not None else None
    return OperationRecord(
        id=str(row["id"]),
        operation=str(row["operation"]),
        status=str(row["status"]),
        workspace_id=str(row["workspace_id"]),
        sandbox_id=str(row["sandbox_id"]) if row["sandbox_id"] is not None else None,
        idempotency_key=str(row["idempotency_key"]),
        input=json.loads(row["input_json"]),
        result=result,
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )
