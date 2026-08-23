from __future__ import annotations

from typing import Any


class AdapterError(Exception):
    """Base error rendered as an OpenAI-compatible error envelope."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        error_type: str = "server_error",
        code: str = "adapter_error",
        param: str | None = None,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.param = param
        self.details = details

    def envelope(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "message": self.message,
            "type": self.error_type,
            "param": self.param,
            "code": self.code,
        }
        if self.details is not None and self.status_code < 500:
            error["details"] = self.details
        return {"error": error}


class InvalidRequestError(AdapterError):
    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        code: str = "invalid_request",
    ) -> None:
        super().__init__(
            message,
            status_code=400,
            error_type="invalid_request_error",
            code=code,
            param=param,
        )


class ResponseNotFoundError(AdapterError):
    def __init__(self, response_id: str) -> None:
        super().__init__(
            f"Response '{response_id}' was not found.",
            status_code=404,
            error_type="invalid_request_error",
            code="response_not_found",
            param="response_id",
        )


class ResourceNotFoundError(AdapterError):
    def __init__(self, uri: str) -> None:
        super().__init__(
            f"MCP resource '{uri}' was not found.",
            status_code=404,
            error_type="invalid_request_error",
            code="mcp_resource_not_found",
            param="uri",
        )


class InteractionNotFoundError(AdapterError):
    def __init__(self, interaction_id: str) -> None:
        super().__init__(
            f"MCP App interaction '{interaction_id}' was not found.",
            status_code=404,
            error_type="invalid_request_error",
            code="mcp_app_interaction_not_found",
            param="interaction_id",
        )


class ResponseConflictError(AdapterError):
    def __init__(self, message: str, *, code: str = "response_conflict") -> None:
        super().__init__(
            message,
            status_code=409,
            error_type="invalid_request_error",
            code=code,
        )


class UpstreamProtocolError(AdapterError):
    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(
            message,
            status_code=502,
            error_type="server_error",
            code="codex_app_server_error",
            details=details,
        )
