#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

IMAGE_DATA = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z4pAAAAAASUVORK5CYII="
)
APP_SERVER = "image_apps"
APP_TOOL = "edit_image"
APP_URI = "ui://image/editor"
APP_CALL_ID = "mcp_call_image_editor"
ELICITATION_ID = "elicitation_image_editor"
APPROVAL_ID = "approval_command"


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(request_id: int | str, value: Any) -> None:
    send({"id": request_id, "result": value})


def error(request_id: int | str, message: str) -> None:
    send({"id": request_id, "error": {"code": -32601, "message": message}})


def text_from_input(params: dict[str, Any]) -> str:
    values: list[str] = []
    for item in params.get("input") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            values.append(item["text"])
    return "\n".join(values)


def app_context() -> dict[str, Any]:
    return {
        "connectorId": "image-editor",
        "linkId": "image-editor-link",
        "resourceUri": APP_URI,
    }


def mcp_started_item() -> dict[str, Any]:
    return {
        "id": APP_CALL_ID,
        "type": "mcpToolCall",
        "server": APP_SERVER,
        "tool": APP_TOOL,
        "status": "inProgress",
        "arguments": {"imageUrl": "https://example.test/input.png"},
        "appContext": app_context(),
    }


def mcp_completed_item(selection: Any, method: Any, *, cancelled: bool) -> dict[str, Any]:
    if cancelled:
        tool_result: dict[str, Any] = {
            "content": [{"type": "text", "text": "Image edit was cancelled."}],
            "structuredContent": {"cancelled": True},
            "isError": True,
            "_meta": {"ui": {"resourceUri": APP_URI}},
        }
        status = "failed"
    else:
        tool_result = {
            "content": [
                {"type": "text", "text": "Edited image is ready."},
                {"type": "image", "data": IMAGE_DATA, "mimeType": "image/png"},
            ],
            "structuredContent": {
                "image_url": "https://example.test/edited.png",
                "selection": selection,
                "method": method,
            },
            "isError": False,
            "_meta": {"ui": {"resourceUri": APP_URI}},
        }
        status = "completed"
    return {
        "id": APP_CALL_ID,
        "type": "mcpToolCall",
        "server": APP_SERVER,
        "tool": APP_TOOL,
        "status": status,
        "arguments": {"imageUrl": "https://example.test/input.png"},
        "appContext": app_context(),
        "result": tool_result,
    }


def complete_normal_turn(thread_id: str, turn_id: str) -> None:
    for delta in ("hello ", "world"):
        send(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemId": "msg_fake",
                    "delta": delta,
                },
            }
        )
    message = {"id": "msg_fake", "type": "agentMessage", "text": "hello world"}
    send(
        {
            "method": "item/completed",
            "params": {"threadId": thread_id, "turnId": turn_id, "item": message},
        }
    )
    complete_turn(thread_id, turn_id, [message])


def complete_mcp_turn(
    thread_id: str,
    turn_id: str,
    selection: Any,
    method: Any,
    *,
    cancelled: bool,
) -> None:
    mcp_item = mcp_completed_item(selection, method, cancelled=cancelled)
    send(
        {
            "method": "item/completed",
            "params": {"threadId": thread_id, "turnId": turn_id, "item": mcp_item},
        }
    )
    text = "Image edit cancelled." if cancelled else "The edited image is ready."
    message = {"id": "msg_image", "type": "agentMessage", "text": text}
    send(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "msg_image",
                "delta": text,
            },
        }
    )
    send(
        {
            "method": "item/completed",
            "params": {"threadId": thread_id, "turnId": turn_id, "item": message},
        }
    )
    complete_turn(thread_id, turn_id, [mcp_item, message])


def complete_turn(thread_id: str, turn_id: str, items: list[dict[str, Any]]) -> None:
    send(
        {
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {
                    "id": turn_id,
                    "status": "completed",
                    "items": items,
                    "usage": {
                        "inputTokens": 7,
                        "cachedInputTokens": 2,
                        "outputTokens": 2,
                    },
                },
            },
        }
    )


def app_html() -> str:
    return """<!doctype html>
<html>
<head><meta charset=\"utf-8\"><title>Image Editor MCP App</title></head>
<body>
  <main>
    <canvas id=\"image-editor\" width=\"640\" height=\"360\"></canvas>
    <label>Method
      <select id=\"method\">
        <option>blur</option><option>remove</option><option>replace</option>
      </select>
    </label>
    <button id=\"apply\">Apply</button>
  </main>
</body>
</html>"""


def main() -> None:
    thread_id = "thread_test"
    pending_mcp: dict[str, Any] | None = None
    pending_cancel: dict[str, str] | None = None
    pending_approval: dict[str, str] | None = None
    initialize_capabilities: dict[str, Any] = {}

    for raw_line in sys.stdin:
        message = json.loads(raw_line)
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        # Response to a server-initiated elicitation request.
        if method is None and request_id == APPROVAL_ID and pending_approval is not None:
            outcome = "rejected" if "error" in message else "approved"
            send(
                {
                    "method": f"test/approval-{outcome}",
                    "params": {
                        "threadId": pending_approval["thread_id"],
                        "turnId": pending_approval["turn_id"],
                    },
                }
            )
            complete_turn(pending_approval["thread_id"], pending_approval["turn_id"], [])
            pending_approval = None
            continue

        if method is None and request_id == ELICITATION_ID and pending_mcp is not None:
            elicitation_result = message.get("result") or {}
            action = elicitation_result.get("action")
            content = elicitation_result.get("content") or {}
            cancelled = action != "accept"
            complete_mcp_turn(
                pending_mcp["thread_id"],
                pending_mcp["turn_id"],
                content.get("selection"),
                content.get("method"),
                cancelled=cancelled,
            )
            pending_mcp = None
            continue

        if method == "initialize" and isinstance(request_id, (int, str)):
            initialize_capabilities = params.get("capabilities") or {}
            result(
                request_id,
                {
                    "userAgent": "fake-codex",
                    "codexHome": "/tmp/fake-codex",
                    "platformFamily": "unix",
                    "platformOs": "linux",
                },
            )
        elif method == "initialized":
            continue
        elif method in {"thread/start", "thread/fork"} and isinstance(request_id, (int, str)):
            thread_id = f"thread_{uuid.uuid4().hex[:8]}"
            result(
                request_id,
                {
                    "thread": {"id": thread_id},
                    "model": "fake-codex",
                    "modelProvider": "fake",
                    "cwd": params.get("cwd", "/tmp"),
                },
            )
        elif method == "thread/resume" and isinstance(request_id, (int, str)):
            thread_id = str(params.get("threadId") or thread_id)
            result(
                request_id,
                {
                    "thread": {"id": thread_id},
                    "model": "fake-codex",
                    "modelProvider": "fake",
                    "cwd": params.get("cwd", "/tmp"),
                },
            )
        elif method == "turn/start" and isinstance(request_id, (int, str)):
            turn_id = f"turn_{uuid.uuid4().hex[:8]}"
            result(
                request_id,
                {"turn": {"id": turn_id, "status": "inProgress", "items": []}},
            )
            send(
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": turn_id, "status": "inProgress", "items": []},
                    },
                }
            )
            prompt = text_from_input(params).lower()
            if "wait until cancelled" in prompt:
                pending_cancel = {"thread_id": thread_id, "turn_id": turn_id}
                continue
            if "request shell approval" in prompt:
                pending_approval = {"thread_id": thread_id, "turn_id": turn_id}
                send(
                    {
                        "id": APPROVAL_ID,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "itemId": "command_test",
                            "command": "echo unsafe",
                        },
                    }
                )
                continue
            if "mcp app" not in prompt and "image editor" not in prompt:
                complete_normal_turn(thread_id, turn_id)
                continue

            extensions = initialize_capabilities.get("extensions") or {}
            if "io.modelcontextprotocol/ui" not in extensions:
                error(request_id, "MCP Apps extension was not advertised")
                continue

            send(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": mcp_started_item(),
                    },
                }
            )
            pending_mcp = {"thread_id": thread_id, "turn_id": turn_id}
            send(
                {
                    "id": ELICITATION_ID,
                    "method": "mcpServer/elicitation/request",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "serverName": APP_SERVER,
                        "mode": "form",
                        "message": "Select the image region and edit method.",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {
                                "selection": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "number"},
                                        "y": {"type": "number"},
                                        "width": {"type": "number"},
                                        "height": {"type": "number"},
                                    },
                                    "required": ["x", "y", "width", "height"],
                                },
                                "method": {
                                    "type": "string",
                                    "enum": ["blur", "remove", "replace"],
                                },
                            },
                            "required": ["selection", "method"],
                        },
                    },
                }
            )
        elif method == "app/read" and isinstance(request_id, (int, str)):
            app_ids = params.get("appIds") or []
            result(
                request_id,
                {
                    "apps": [
                        {
                            "id": app_id,
                            "name": "Image editor",
                            "description": "Edit an image",
                            "iconUrl": None,
                            "iconUrlDark": None,
                            "distributionChannel": None,
                            "installUrl": None,
                            "pluginDisplayNames": [],
                            "toolSummaries": [
                                {
                                    "name": "apply_edit",
                                    "title": "Apply edit",
                                    "description": "Apply the selected edit",
                                    "isEnabled": True,
                                    "disabledReason": None,
                                    "isReadOnly": False,
                                }
                            ],
                        }
                        for app_id in app_ids
                    ],
                    "missingAppIds": [],
                },
            )
        elif method == "test/environment" and isinstance(request_id, (int, str)):
            result(
                request_id,
                {
                    "sandbox_worker_api_key": os.environ.get("SANDBOX_WORKER_API_KEY"),
                    "http_proxy": os.environ.get("HTTP_PROXY"),
                },
            )
        elif method == "mcpServer/resource/read" and isinstance(request_id, (int, str)):
            result(
                request_id,
                {
                    "contents": [
                        {
                            "uri": params.get("uri", APP_URI),
                            "mimeType": "text/html;profile=mcp-app",
                            "text": app_html(),
                            "_meta": {
                                "ui": {
                                    "csp": {
                                        "resourceDomains": [],
                                        "connectDomains": [],
                                        "frameDomains": [],
                                    }
                                }
                            },
                        }
                    ],
                    "originCallId": params.get("originCallId"),
                },
            )
        elif method == "mcpServer/tool/call" and isinstance(request_id, (int, str)):
            arguments = params.get("arguments") or {}
            result(
                request_id,
                {
                    "content": [{"type": "text", "text": "Edited image is ready."}],
                    "structuredContent": {
                        "image_url": "https://example.test/edited-from-app.png",
                        "selection": arguments.get("selection"),
                        "method": arguments.get("method"),
                    },
                    "isError": False,
                    "_meta": {"ui": {"resourceUri": APP_URI}},
                },
            )
        elif method == "turn/interrupt" and isinstance(request_id, (int, str)):
            result(request_id, {})
            if pending_cancel is not None:
                send(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": pending_cancel["thread_id"],
                            "turn": {
                                "id": pending_cancel["turn_id"],
                                "status": "interrupted",
                                "items": [],
                            },
                        },
                    }
                )
                pending_cancel = None
        elif isinstance(request_id, (int, str)):
            error(request_id, f"unknown method: {method}")
        time.sleep(0.001)


if __name__ == "__main__":
    main()
