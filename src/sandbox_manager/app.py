from __future__ import annotations

# FastAPI registers decorated local handlers dynamically.
# pyright: reportUnusedFunction=false
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sandbox_api import install_bearer_auth

from .backend import SandboxBackend, SandboxBackendError, SandboxNotFoundError
from .models import SandboxInfo
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

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sandboxes", response_model=SandboxInfo, status_code=201)
    async def create_sandbox() -> SandboxInfo:
        return await sandbox_backend.create()

    @app.get("/v1/sandboxes/{sandbox_id}", response_model=SandboxInfo)
    async def inspect_sandbox(sandbox_id: str) -> SandboxInfo:
        return await sandbox_backend.inspect(sandbox_id)

    @app.post("/v1/sandboxes/{sandbox_id}/lease", response_model=SandboxInfo)
    async def renew_sandbox(sandbox_id: str) -> SandboxInfo:
        return await sandbox_backend.renew(sandbox_id)

    @app.delete("/v1/sandboxes/{sandbox_id}", response_model=SandboxInfo)
    async def terminate_sandbox(sandbox_id: str) -> SandboxInfo:
        return await sandbox_backend.terminate(sandbox_id)

    return app
