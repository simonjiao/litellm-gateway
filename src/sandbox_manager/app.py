from __future__ import annotations

# FastAPI registers decorated local handlers dynamically.
# pyright: reportUnusedFunction=false
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sandbox_api import install_bearer_auth

from .backend import (
    SandboxAuthorizationError,
    SandboxBackend,
    SandboxBackendError,
    SandboxConflictError,
    SandboxNotFoundError,
    WorkspaceNotFoundError,
)
from .models import (
    SandboxCreateRequest,
    SandboxInfo,
    WorkspaceCreateRequest,
    WorkspaceGrantRequest,
    WorkspaceInfo,
)
from .settings import ManagerSettings

logger = logging.getLogger(__name__)


def create_app(
    settings: ManagerSettings | None = None,
    *,
    backend: SandboxBackend | None = None,
) -> FastAPI:
    runtime_settings = settings or ManagerSettings()
    if backend is None:
        from .docker_backend import DockerSandboxBackend

        sandbox_backend: SandboxBackend = DockerSandboxBackend(runtime_settings)
    else:
        sandbox_backend = backend

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.backend = sandbox_backend
        await sandbox_backend.startup()
        try:
            yield
        finally:
            await sandbox_backend.shutdown()

    app = FastAPI(title="Sandbox Manager", version="0.3.0", lifespan=lifespan)
    install_bearer_auth(app, runtime_settings.api_key)

    @app.exception_handler(SandboxNotFoundError)
    async def not_found(_: Request, exc: SandboxNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": str(exc),
                    "code": "sandbox_not_found",
                }
            },
        )

    @app.exception_handler(SandboxBackendError)
    async def backend_error(_: Request, exc: SandboxBackendError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "code": "sandbox_backend_error"}},
        )

    @app.exception_handler(SandboxAuthorizationError)
    async def authorization_error(_: Request, exc: SandboxAuthorizationError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"error": {"message": str(exc), "code": "workspace_grant_invalid"}},
        )

    @app.exception_handler(SandboxConflictError)
    async def conflict_error(_: Request, exc: SandboxConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": {"message": str(exc), "code": "sandbox_conflict"}},
        )

    @app.exception_handler(WorkspaceNotFoundError)
    async def workspace_not_found(_: Request, exc: WorkspaceNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": str(exc), "code": "workspace_not_found"}},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sandboxes", response_model=SandboxInfo, status_code=201)
    async def create_sandbox(body: SandboxCreateRequest | None = None) -> SandboxInfo:
        if body is None or body.workspace_grant is None:
            return await sandbox_backend.create()
        return await sandbox_backend.create(body.workspace_grant)

    @app.get("/v1/sandboxes/{sandbox_id}", response_model=SandboxInfo)
    async def inspect_sandbox(sandbox_id: str) -> SandboxInfo:
        return await sandbox_backend.inspect(sandbox_id)

    @app.post("/v1/sandboxes/{sandbox_id}/lease", response_model=SandboxInfo)
    async def renew_sandbox(sandbox_id: str) -> SandboxInfo:
        return await sandbox_backend.renew(sandbox_id)

    @app.delete("/v1/sandboxes/{sandbox_id}", response_model=SandboxInfo)
    async def terminate_sandbox(sandbox_id: str) -> SandboxInfo:
        return await sandbox_backend.terminate(sandbox_id)

    @app.post("/v1/workspaces", response_model=WorkspaceInfo, status_code=201)
    async def create_workspace(body: WorkspaceCreateRequest) -> WorkspaceInfo:
        return await sandbox_backend.create_workspace(body.workspace_id, body.grant)

    @app.post("/v1/workspaces/{workspace_id}/inspect", response_model=WorkspaceInfo)
    async def inspect_workspace(workspace_id: str, body: WorkspaceGrantRequest) -> WorkspaceInfo:
        return await sandbox_backend.inspect_workspace(workspace_id, body.grant)

    @app.post("/v1/workspaces/{workspace_id}/release", response_model=WorkspaceInfo)
    async def release_workspace(workspace_id: str, body: WorkspaceGrantRequest) -> WorkspaceInfo:
        return await sandbox_backend.release_workspace(workspace_id, body.grant)

    return app
