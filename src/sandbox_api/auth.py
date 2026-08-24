"""Bearer authentication shared by Sandbox HTTP APIs."""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response


def install_bearer_auth(app: FastAPI, expected: str) -> None:
    async def authenticate(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path != "/healthz" and not _valid_bearer(
            request.headers.get("authorization"), expected
        ):
            return _unauthorized_response()
        return await call_next(request)

    app.middleware("http")(authenticate)


def _valid_bearer(authorization: str | None, expected: str) -> bool:
    if authorization is None or not authorization.startswith("Bearer "):
        return False
    supplied = authorization.removeprefix("Bearer ")
    return secrets.compare_digest(supplied.encode(), expected.encode())


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"message": "Unauthorized", "code": "unauthorized"}},
        headers={"WWW-Authenticate": "Bearer"},
    )
