from __future__ import annotations

import base64
from typing import Any

from fastapi.responses import Response

from .errors import InvalidRequestError, ResourceNotFoundError

MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"


def render_mcp_resource(
    payload: dict[str, Any],
    *,
    requested_uri: str,
    max_bytes: int,
) -> Response:
    contents = payload.get("contents")
    if not isinstance(contents, list):
        raise ResourceNotFoundError(requested_uri)

    selected: dict[str, Any] | None = None
    for content in contents:
        if isinstance(content, dict) and content.get("uri") == requested_uri:
            selected = content
            break
    if selected is None:
        selected = next((item for item in contents if isinstance(item, dict)), None)
    if selected is None:
        raise ResourceNotFoundError(requested_uri)

    mime_type = selected.get("mimeType") or selected.get("mime_type")
    if not isinstance(mime_type, str):
        mime_type = "application/octet-stream"

    raw: bytes
    text = selected.get("text")
    blob = selected.get("blob")
    if isinstance(text, str):
        raw = text.encode("utf-8")
    elif isinstance(blob, str):
        try:
            raw = base64.b64decode(blob, validate=True)
        except ValueError as exc:
            raise InvalidRequestError(
                "MCP resource blob is not valid base64.",
                param="blob",
                code="invalid_mcp_resource_blob",
            ) from exc
    else:
        raise InvalidRequestError(
            "MCP resource did not contain text or blob content.",
            code="invalid_mcp_resource",
        )

    if len(raw) > max_bytes:
        raise InvalidRequestError(
            "MCP App resource exceeds the configured size limit.",
            code="mcp_app_resource_too_large",
        )

    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
    }
    if mime_type.lower().startswith(MCP_APP_MIME_TYPE):
        headers["Content-Security-Policy"] = build_mcp_app_csp(selected.get("_meta"))
    return Response(content=raw, media_type=mime_type, headers=headers)


def build_mcp_app_csp(meta: Any) -> str:
    ui_meta = _ui_meta(meta)
    raw_csp = ui_meta.get("csp")
    csp: dict[str, Any] = raw_csp if isinstance(raw_csp, dict) else {}

    resource_domains = _safe_sources(csp.get("resourceDomains"))
    connect_domains = _safe_sources(csp.get("connectDomains"))
    frame_domains = _safe_sources(csp.get("frameDomains"))
    base_uri_domains = _safe_sources(csp.get("baseUriDomains"))

    directives = [
        "default-src 'none'",
        _directive("script-src", ["'self'", "'unsafe-inline'", *resource_domains]),
        _directive("style-src", ["'self'", "'unsafe-inline'", *resource_domains]),
        _directive("img-src", ["'self'", "data:", "blob:", *resource_domains]),
        _directive("font-src", ["'self'", "data:", *resource_domains]),
        _directive("media-src", ["'self'", "data:", "blob:", *resource_domains]),
        _directive("worker-src", ["'self'", "blob:", *resource_domains]),
        _directive("connect-src", connect_domains or ["'none'"]),
        _directive("frame-src", frame_domains or ["'none'"]),
        _directive("base-uri", base_uri_domains or ["'none'"]),
        "object-src 'none'",
        "form-action 'none'",
    ]
    return "; ".join(directives) + ";"


def _ui_meta(meta: Any) -> dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    ui = meta.get("ui")
    result = dict(ui) if isinstance(ui, dict) else {}
    # Deprecated flat MCP Apps metadata is accepted for compatibility.
    if "csp" not in result and isinstance(meta.get("ui/csp"), dict):
        result["csp"] = meta["ui/csp"]
    if "permissions" not in result and isinstance(meta.get("ui/permissions"), dict):
        result["permissions"] = meta["ui/permissions"]
    return result


def _safe_sources(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        source = item.strip()
        if not source or any(char in source for char in (";", "\n", "\r")):
            continue
        result.append(source)
    return result


def _directive(name: str, sources: list[str]) -> str:
    return f"{name} {' '.join(dict.fromkeys(sources))}"
