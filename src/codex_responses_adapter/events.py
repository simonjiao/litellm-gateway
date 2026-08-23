from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from .models import ResponseRecord


class ResponsesEventBuilder:
    """Translate Codex app-server notifications into Responses SSE events."""

    def __init__(self, record: ResponseRecord) -> None:
        self.record = record
        self._sequence_number = 0
        self._terminal = False
        self._completed_mcp_calls: set[str] = set()

    @property
    def terminal(self) -> bool:
        return self._terminal

    def initial_events(self) -> list[dict[str, Any]]:
        return [
            self._event("response.created", response=self.record.to_response()),
            self._event("response.in_progress", response=self.record.to_response()),
        ]

    def consume(self, notification: dict[str, Any]) -> list[dict[str, Any]]:
        method = notification.get("method")
        params = notification.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return []

        if method == "item/started":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "mcpToolCall":
                return self._start_mcp_call(item)
            return []

        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if not isinstance(delta, str) or not delta:
                return []
            item_id = params.get("itemId")
            return self._append_text(delta, item_id if isinstance(item_id, str) else None)

        if method == "item/completed":
            item = params.get("item")
            if not isinstance(item, dict):
                return []
            if item.get("type") == "agentMessage":
                text = item.get("text")
                item_id = item.get("id")
                return self._sync_completed_message(
                    text if isinstance(text, str) else "",
                    item_id if isinstance(item_id, str) else None,
                )
            if item.get("type") == "mcpToolCall":
                return self._complete_mcp_call(item)
            return []

        if method == "turn/completed":
            turn = params.get("turn")
            return self._complete_turn(turn if isinstance(turn, dict) else {})

        if method in {"error", "turn/error"}:
            message = _error_message(params)
            return self.fail(message)

        return []

    def fail(self, message: str) -> list[dict[str, Any]]:
        if self._terminal:
            return []
        events = self._finish_message()
        self.record.status = "failed"
        self.record.error = {
            "code": "codex_execution_failed",
            "message": message,
        }
        self._terminal = True
        events.append(
            self._event(
                "response.failed",
                response=self.record.to_response(),
            )
        )
        return events

    def incomplete(self, reason: str) -> list[dict[str, Any]]:
        if self._terminal:
            return []
        events = self._finish_message()
        self.record.status = "incomplete"
        self.record.incomplete_details = {"reason": reason}
        self._terminal = True
        events.append(
            self._event(
                "response.incomplete",
                response=self.record.to_response(),
            )
        )
        return events

    def _start_mcp_call(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        call_id = _string(source.get("id"))
        if not call_id:
            return []
        if self.record.output_index(call_id) is not None:
            return []

        item = self._mcp_call_item(source, completed=False)
        output_index = self.record.add_output_item(item)
        arguments = item["arguments"]
        return [
            self._event(
                "response.output_item.added",
                output_index=output_index,
                item=item,
            ),
            self._event(
                "response.mcp_call_arguments.done",
                item_id=call_id,
                output_index=output_index,
                arguments=arguments,
            ),
            self._event(
                "response.mcp_call.in_progress",
                item_id=call_id,
                output_index=output_index,
            ),
        ]

    def _complete_mcp_call(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        call_id = _string(source.get("id"))
        if not call_id or call_id in self._completed_mcp_calls:
            return []

        events: list[dict[str, Any]] = []
        if self.record.output_index(call_id) is None:
            events.extend(self._start_mcp_call(source))

        item = self._mcp_call_item(source, completed=True)
        output_index = self.record.update_output_item(item)
        failed = item.get("status") == "failed"
        events.append(
            self._event(
                "response.mcp_call.failed" if failed else "response.mcp_call.completed",
                item_id=call_id,
                output_index=output_index,
            )
        )
        events.append(
            self._event(
                "response.output_item.done",
                output_index=output_index,
                item=item,
            )
        )
        self._completed_mcp_calls.add(call_id)
        return events

    def _mcp_call_item(
        self,
        source: dict[str, Any],
        *,
        completed: bool,
    ) -> dict[str, Any]:
        call_id = _string(source.get("id")) or "mcp_unknown"
        server = _string(source.get("server")) or "unknown"
        tool = _string(source.get("tool")) or "unknown"
        arguments_value = source.get("arguments")
        arguments = _json_string(arguments_value if arguments_value is not None else {})

        error = source.get("error")
        result = source.get("result")
        is_error = isinstance(result, dict) and result.get("isError") is True
        failed = error is not None or is_error or _status(source.get("status")) == "failed"
        status = "failed" if failed else ("completed" if completed else "in_progress")

        item: dict[str, Any] = {
            "id": call_id,
            "type": "mcp_call",
            "arguments": arguments,
            "name": tool,
            "server_label": server,
            "status": status,
        }
        if completed and result is not None:
            item["output"] = _json_string(result)
        if failed:
            item["error"] = _mcp_error(error if error is not None else result)

        meta = self._mcp_meta(source, call_id=call_id, server=server, tool=tool)
        if isinstance(result, dict) and isinstance(result.get("_meta"), dict):
            meta["mcp_result"] = result["_meta"]
        if meta:
            item["_meta"] = meta
        return item

    def _mcp_meta(
        self,
        source: dict[str, Any],
        *,
        call_id: str,
        server: str,
        tool: str,
    ) -> dict[str, Any]:
        app_context = source.get("appContext")
        if not isinstance(app_context, dict):
            app_context = {}
        resource_uri = (
            _string(app_context.get("resourceUri"))
            or _string(source.get("mcpAppResourceUri"))
            or _resource_uri_from_result(source.get("result"))
        )
        if not resource_uri:
            return {"codex_app_context": app_context} if app_context else {}

        connector_id = _string(app_context.get("connectorId"))
        link_id = _string(app_context.get("linkId"))
        query = {
            "server": server,
            "uri": resource_uri,
            "origin_call_id": call_id,
        }
        if connector_id:
            query["connector_id"] = connector_id

        resource_path = f"/v1/mcp-apps/responses/{self.record.id}/resources?{urlencode(query)}"
        events_path = f"/v1/mcp-apps/responses/{self.record.id}/events"
        state_path = f"/v1/mcp-apps/responses/{self.record.id}/state"
        tool_call_path = f"/v1/mcp-apps/responses/{self.record.id}/tools/call"
        descriptor = {
            "response_id": self.record.id,
            "call_id": call_id,
            "server": server,
            "tool": tool,
            "resource_uri": resource_uri,
            "resource_url": self._public_url(resource_path),
            "events_url": self._public_url(events_path),
            "state_url": self._public_url(state_path),
            "tool_call_url": self._public_url(tool_call_path),
            "connector_id": connector_id,
            "link_id": link_id,
            "app_id": connector_id or link_id or call_id,
            # Filled from app/read by the service, never from client input.
            "allowed_tools": [],
        }
        self.record.register_mcp_app_target(call_id, descriptor)
        return {
            "ui": {"resourceUri": resource_uri},
            "mcp_app": descriptor,
            "codex_app_context": app_context,
        }

    def _public_url(self, path: str) -> str:
        base = self.record.mcp_apps_base_url.rstrip("/")
        return f"{base}{path}" if base else path

    def _append_text(self, delta: str, item_id: str | None) -> list[dict[str, Any]]:
        events = self._start_message(item_id)
        self.record.output_text += delta
        self.record.sync_message_item()
        output_index = self.record.message_output_index or 0
        events.append(
            self._event(
                "response.output_text.delta",
                item_id=self.record.message_id,
                output_index=output_index,
                content_index=0,
                delta=delta,
            )
        )
        return events

    def _sync_completed_message(self, text: str, item_id: str | None) -> list[dict[str, Any]]:
        events = self._start_message(item_id)
        if text and text != self.record.output_text:
            if text.startswith(self.record.output_text):
                missing = text[len(self.record.output_text) :]
                if missing:
                    self.record.output_text += missing
                    self.record.sync_message_item()
                    events.append(
                        self._event(
                            "response.output_text.delta",
                            item_id=self.record.message_id,
                            output_index=self.record.message_output_index or 0,
                            content_index=0,
                            delta=missing,
                        )
                    )
            else:
                self.record.output_text = text
                self.record.sync_message_item()
        return events

    def _complete_turn(self, turn: dict[str, Any]) -> list[dict[str, Any]]:
        if self._terminal:
            return []

        events: list[dict[str, Any]] = []
        items = turn.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("type") == "mcpToolCall":
                    events.extend(self._complete_mcp_call(item))

        final_text, item_id = _last_agent_message(turn)
        if final_text:
            events.extend(self._sync_completed_message(final_text, item_id))

        events.extend(self._finish_message())
        self.record.usage = _usage_from_turn(turn)
        status = _status(turn.get("status")) or "completed"

        if status == "completed":
            self.record.status = "completed"
            self._terminal = True
            events.append(
                self._event(
                    "response.completed",
                    response=self.record.to_response(),
                )
            )
            return events

        if status == "interrupted":
            self.record.status = "incomplete"
            self.record.incomplete_details = {"reason": "cancelled"}
            self._terminal = True
            events.append(
                self._event(
                    "response.incomplete",
                    response=self.record.to_response(),
                )
            )
            return events

        error = turn.get("error")
        self.record.status = "failed"
        self.record.error = {
            "code": "codex_turn_failed",
            "message": _error_message(error if isinstance(error, dict) else turn),
        }
        self._terminal = True
        events.append(
            self._event(
                "response.failed",
                response=self.record.to_response(),
            )
        )
        return events

    def _start_message(self, item_id: str | None) -> list[dict[str, Any]]:
        if self.record.message_started:
            return []
        output_index = self.record.ensure_message(item_id)
        message = self.record.message_item()
        return [
            self._event(
                "response.output_item.added",
                output_index=output_index,
                item=message,
            ),
            self._event(
                "response.content_part.added",
                item_id=self.record.message_id,
                output_index=output_index,
                content_index=0,
                part={"type": "output_text", "text": "", "annotations": []},
            ),
        ]

    def _finish_message(self) -> list[dict[str, Any]]:
        if not self.record.message_started or self.record.message_completed:
            return []
        self.record.message_completed = True
        self.record.sync_message_item()
        output_index = self.record.message_output_index or 0
        return [
            self._event(
                "response.output_text.done",
                item_id=self.record.message_id,
                output_index=output_index,
                content_index=0,
                text=self.record.output_text,
            ),
            self._event(
                "response.content_part.done",
                item_id=self.record.message_id,
                output_index=output_index,
                content_index=0,
                part={
                    "type": "output_text",
                    "text": self.record.output_text,
                    "annotations": [],
                },
            ),
            self._event(
                "response.output_item.done",
                output_index=output_index,
                item=self.record.message_item(),
            ),
        ]

    def _event(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event = {
            "type": event_type,
            "sequence_number": self._sequence_number,
            **payload,
        }
        self._sequence_number += 1
        return event


def mcp_app_side_event(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    event_type = event.get("type")
    if event_type == "response.output_item.added":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "mcp_call":
            meta = item.get("_meta")
            if isinstance(meta, dict) and isinstance(meta.get("mcp_app"), dict):
                return "mcp_app.available", {
                    "item": item,
                    "output_index": event.get("output_index"),
                }
    if event_type == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "mcp_call":
            meta = item.get("_meta")
            if isinstance(meta, dict) and isinstance(meta.get("mcp_app"), dict):
                return "mcp_app.tool.completed", {
                    "item": item,
                    "output_index": event.get("output_index"),
                }
    return None


def _resource_uri_from_result(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    meta = value.get("_meta")
    if not isinstance(meta, dict):
        return None
    ui = meta.get("ui")
    if isinstance(ui, dict):
        resource_uri = _string(ui.get("resourceUri"))
        if resource_uri:
            return resource_uri
    return _string(meta.get("ui/resourceUri"))


def _last_agent_message(turn: dict[str, Any]) -> tuple[str, str | None]:
    items = turn.get("items")
    if not isinstance(items, list):
        return "", None
    for item in reversed(items):
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        item_id = item.get("id")
        return (
            text if isinstance(text, str) else "",
            item_id if isinstance(item_id, str) else None,
        )
    return "", None


def _usage_from_turn(turn: dict[str, Any]) -> dict[str, Any] | None:
    raw = turn.get("usage")
    if not isinstance(raw, dict):
        return None
    input_tokens = _as_int(raw.get("inputTokens") or raw.get("input_tokens"))
    cached_tokens = _as_int(raw.get("cachedInputTokens") or raw.get("cached_input_tokens"))
    output_tokens = _as_int(raw.get("outputTokens") or raw.get("output_tokens"))
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _error_message(value: dict[str, Any]) -> str:
    for key in ("message", "error", "detail"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, dict):
            nested = candidate.get("message")
            if isinstance(nested, str) and nested:
                return nested
    return "Codex app-server execution failed"


def _mcp_error(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        code = value.get("code")
        if isinstance(code, int) and not isinstance(code, bool):
            return {
                "type": "mcp_protocol_error",
                "code": code,
                "message": _error_message(value),
            }
        return {"type": "mcp_tool_execution_error", "content": value}
    if isinstance(value, str):
        return {"type": "mcp_tool_execution_error", "content": value}
    return {
        "type": "mcp_tool_execution_error",
        "content": "MCP tool call failed",
    }


def _json_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("_", "").replace("-", "").lower()
    return {
        "inprogress": "in_progress",
        "calling": "in_progress",
        "completed": "completed",
        "failed": "failed",
        "interrupted": "interrupted",
        "incomplete": "incomplete",
    }.get(normalized, value)
