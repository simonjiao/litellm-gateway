from __future__ import annotations

from codex_responses_adapter.events import ResponsesEventBuilder
from codex_responses_adapter.models import CreateResponseRequest, ResponseRecord


def test_maps_agent_text_and_completion() -> None:
    request = CreateResponseRequest(model="codex-app-server", input="hello")
    record = ResponseRecord.create(request, [])
    builder = ResponsesEventBuilder(record)

    initial = builder.initial_events()
    assert [event["type"] for event in initial] == [
        "response.created",
        "response.in_progress",
    ]

    deltas = builder.consume(
        {
            "method": "item/agentMessage/delta",
            "params": {"itemId": "msg_1", "delta": "hello"},
        }
    )
    assert deltas[-1]["type"] == "response.output_text.delta"

    terminal = builder.consume(
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": "turn_1",
                    "status": "completed",
                    "items": [{"id": "msg_1", "type": "agentMessage", "text": "hello"}],
                    "usage": {"inputTokens": 3, "outputTokens": 1},
                }
            },
        }
    )
    assert terminal[-1]["type"] == "response.completed"
    assert record.status == "completed"
    assert record.output_text == "hello"
    assert record.usage is not None
    assert record.usage["total_tokens"] == 4


def test_maps_mcp_app_call_to_standard_responses_events() -> None:
    request = CreateResponseRequest(model="codex-app-server", input="edit image", stream=True)
    record = ResponseRecord.create(
        request,
        [],
        mcp_apps_base_url="https://adapter.example",
    )
    builder = ResponsesEventBuilder(record)

    started = builder.consume(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "id": "call_1",
                    "type": "mcpToolCall",
                    "server": "image_apps",
                    "tool": "edit_image",
                    "status": "inProgress",
                    "arguments": {"image": "a.png"},
                    "appContext": {
                        "connectorId": "images",
                        "resourceUri": "ui://image/editor",
                    },
                }
            },
        }
    )
    assert [event["type"] for event in started] == [
        "response.output_item.added",
        "response.mcp_call_arguments.done",
        "response.mcp_call.in_progress",
    ]
    app_descriptor = started[0]["item"]["_meta"]["mcp_app"]
    assert app_descriptor["resource_url"].startswith("https://adapter.example/")

    completed = builder.consume(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "call_1",
                    "type": "mcpToolCall",
                    "server": "image_apps",
                    "tool": "edit_image",
                    "status": "completed",
                    "arguments": {"image": "a.png"},
                    "appContext": {
                        "connectorId": "images",
                        "resourceUri": "ui://image/editor",
                    },
                    "result": {
                        "content": [{"type": "text", "text": "done"}],
                        "structuredContent": {"image_url": "edited.png"},
                    },
                }
            },
        }
    )
    assert [event["type"] for event in completed] == [
        "response.mcp_call.completed",
        "response.output_item.done",
    ]
    assert completed[-1]["item"]["status"] == "completed"
