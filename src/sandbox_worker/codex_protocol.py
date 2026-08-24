from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol, TypeAlias

logger = logging.getLogger(__name__)

RequestId: TypeAlias = int | str
ServerRequestHandler: TypeAlias = Callable[[RequestId, str, dict[str, Any]], Awaitable[Any]]


class CodexSessionSettings(Protocol):
    codex_workdir: Path
    request_timeout_seconds: float
    process_shutdown_seconds: float
    mcp_apps_enabled: bool
    client_name: str
    client_title: str
    client_version: str

    def command_argv(self) -> list[str]: ...


class CodexProtocolError(Exception):
    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class CodexRpcError(CodexProtocolError):
    def __init__(self, method: str, error: Any) -> None:
        super().__init__(f"Codex app-server RPC '{method}' failed", details=error)
        self.method = method
        self.rpc_error = error


class CodexAppServerSession:
    """One initialized Codex app-server JSONL session, owned by a Sandbox Worker."""

    def __init__(
        self,
        settings: CodexSessionSettings,
        *,
        server_request_handler: ServerRequestHandler | None = None,
    ) -> None:
        self._settings = settings
        self._server_request_handler = server_request_handler
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, tuple[str, asyncio.Future[Any]]] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._server_tasks: set[asyncio.Task[None]] = set()
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._closed = False
        self._stderr_tail: deque[str] = deque(maxlen=50)

    async def __aenter__(self) -> CodexAppServerSession:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._process is not None:
            return
        if not self._settings.codex_workdir.exists():
            raise CodexProtocolError(
                f"Configured Codex working directory does not exist: {self._settings.codex_workdir}"
            )
        argv = self._settings.command_argv()
        child_environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("SANDBOX_WORKER_")
        }
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._settings.codex_workdir),
                env=child_environment,
            )
        except FileNotFoundError as exc:
            raise CodexProtocolError(
                f"Unable to start Codex app-server; command not found: {argv[0]}"
            ) from exc
        except OSError as exc:
            raise CodexProtocolError(f"Unable to start Codex app-server: {exc}") from exc

        self._reader_task = asyncio.create_task(self._read_stdout(), name="codex-app-server-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="codex-app-server-stderr")
        initialize_params: dict[str, Any] = {
            "clientInfo": {
                "name": self._settings.client_name,
                "title": self._settings.client_title,
                "version": self._settings.client_version,
            },
            "capabilities": {
                "experimentalApi": self._settings.mcp_apps_enabled,
                "requestAttestation": False,
            },
        }
        if self._settings.mcp_apps_enabled:
            capabilities = initialize_params["capabilities"]
            assert isinstance(capabilities, dict)
            capabilities["extensions"] = {
                "openai/form": {},
                "io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]},
            }
        await self.request("initialize", initialize_params)
        await self.notify("initialized", {})

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        if self._closed:
            raise CodexProtocolError("Codex app-server session is closed")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (method, future)
        await self._write({"id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=self._settings.request_timeout_seconds)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise CodexProtocolError(f"Codex app-server RPC '{method}' timed out") from exc

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def next_notification(self) -> dict[str, Any]:
        message = await self._notifications.get()
        if message.get("method") == "$session/closed":
            params = message.get("params")
            detail = params.get("error") if isinstance(params, dict) else None
            raise CodexProtocolError(
                "Codex app-server closed before the turn completed",
                details=detail or self.stderr_tail,
            )
        return message

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in tuple(self._server_tasks):
            if not task.done():
                task.cancel()
        for task in tuple(self._server_tasks):
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._server_tasks.clear()

        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=self._settings.process_shutdown_seconds
                )
            except TimeoutError:
                process.kill()
                with suppress(Exception):
                    await process.wait()

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await task

        error = CodexProtocolError(
            "Codex app-server session closed", details=self.stderr_tail or None
        )
        for _, future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CodexProtocolError("Codex app-server stdin is unavailable")
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        async with self._write_lock:
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise CodexProtocolError(
                    "Codex app-server connection was closed",
                    details=self.stderr_tail or None,
                ) from exc

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Ignoring non-JSON Codex stdout line: %r", line[:500])
                    continue
                if isinstance(message, dict):
                    await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Codex app-server stdout reader failed")
            await self._notifications.put(
                {"method": "$session/closed", "params": {"error": str(exc)}}
            )
        finally:
            if not self._closed:
                return_code = await process.wait()
                error = {"return_code": return_code, "stderr": self.stderr_tail}
                for _, future in self._pending.values():
                    if not future.done():
                        future.set_exception(
                            CodexProtocolError(
                                "Codex app-server exited while an RPC was pending",
                                details=error,
                            )
                        )
                self._pending.clear()
                await self._notifications.put(
                    {"method": "$session/closed", "params": {"error": error}}
                )

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            while line := await process.stderr.readline():
                text = line.decode(errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    logger.debug("codex app-server: %s", text)
        except asyncio.CancelledError:
            raise

    async def _dispatch(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if isinstance(message_id, int) and ("result" in message or "error" in message):
            pending = self._pending.pop(message_id, None)
            if pending is None:
                return
            method, future = pending
            if future.done():
                return
            if "error" in message:
                future.set_exception(CodexRpcError(method, message["error"]))
            else:
                future.set_result(message.get("result"))
            return

        method = message.get("method")
        if isinstance(message_id, (int, str)) and isinstance(method, str):
            params = message.get("params")
            task = asyncio.create_task(
                self._handle_server_request(
                    message_id,
                    method,
                    params if isinstance(params, dict) else {},
                ),
                name=f"codex-server-request-{method}",
            )
            self._server_tasks.add(task)
            task.add_done_callback(self._server_tasks.discard)
            return
        if isinstance(method, str):
            await self._notifications.put(message)

    async def _handle_server_request(
        self,
        request_id: RequestId,
        method: str,
        params: dict[str, Any],
    ) -> None:
        try:
            if self._server_request_handler is None:
                await self._write(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32601,
                            "message": f"Unsupported app-server request: {method}",
                        },
                    }
                )
                return
            result = await self._server_request_handler(request_id, method, params)
            if not self._closed:
                await self._write({"id": request_id, "result": result})
        except asyncio.CancelledError:
            raise
        except CodexProtocolError as exc:
            if not self._closed:
                await self._write(
                    {
                        "id": request_id,
                        "error": {"code": -32000, "message": exc.message},
                    }
                )
        except Exception:
            logger.exception("Failed to handle Codex app-server request %s", method)
            if not self._closed:
                await self._write(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32603,
                            "message": "Internal MCP Apps bridge error",
                        },
                    }
                )
