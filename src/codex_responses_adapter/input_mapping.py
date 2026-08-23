from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .errors import InvalidRequestError


@dataclass(slots=True)
class MappedInput:
    user_inputs: list[dict[str, Any]]
    developer_instructions: str | None
    input_items: list[dict[str, Any]]


def map_responses_input(value: Any, instructions: str | None) -> MappedInput:
    """Map common Responses input forms to Codex app-server ``UserInput`` values."""

    instruction_parts: list[str] = []
    if instructions:
        instruction_parts.append(instructions.strip())

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise InvalidRequestError("input must not be empty", param="input")
        return MappedInput(
            user_inputs=[_text_input(text)],
            developer_instructions=_join_nonempty(instruction_parts),
            input_items=[_input_message(text)],
        )

    if not isinstance(value, list):
        raise InvalidRequestError(
            "input must be a string or a list of Responses input items",
            param="input",
        )

    transcript: list[str] = []
    images: list[dict[str, Any]] = []

    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise InvalidRequestError(
                f"input[{index}] must be an object",
                param=f"input[{index}]",
            )

        item_type = item.get("type")
        role = item.get("role")

        if item_type == "message" or isinstance(role, str):
            message_role = str(role or "user")
            text_parts, image_parts = _parse_content(item.get("content"), f"input[{index}].content")
            message_text = "\n".join(part for part in text_parts if part).strip()

            if message_role in {"system", "developer"}:
                if message_text:
                    instruction_parts.append(message_text)
                if image_parts:
                    raise InvalidRequestError(
                        f"{message_role} messages cannot contain images",
                        param=f"input[{index}].content",
                    )
                continue

            if message_text:
                transcript.append(f"[{message_role}]\n{message_text}")
            images.extend(image_parts)
            continue

        if item_type in {"input_text", "text"}:
            text = item.get("text")
            if not isinstance(text, str):
                raise InvalidRequestError(
                    f"input[{index}].text must be a string",
                    param=f"input[{index}].text",
                )
            transcript.append(text)
            continue

        if item_type in {"input_image", "image_url"}:
            images.append(_parse_image(item, f"input[{index}]"))
            continue

        if item_type == "function_call_output":
            output = item.get("output")
            call_id = item.get("call_id") or item.get("callId") or "unknown"
            transcript.append(f"[tool output: {call_id}]\n{_stringify_tool_output(output)}")
            continue

        raise InvalidRequestError(
            f"Unsupported Responses input item type: {item_type!r}",
            param=f"input[{index}].type",
            code="unsupported_input_item",
        )

    user_inputs: list[dict[str, Any]] = []
    joined_transcript = "\n\n".join(part for part in transcript if part.strip()).strip()
    if joined_transcript:
        user_inputs.append(_text_input(joined_transcript))
    user_inputs.extend(images)

    if not user_inputs:
        raise InvalidRequestError("input contains no supported user content", param="input")

    return MappedInput(
        user_inputs=user_inputs,
        developer_instructions=_join_nonempty(instruction_parts),
        input_items=[_stored_input_item(item) for item in value],
    )


def _parse_content(content: Any, param: str) -> tuple[list[str], list[dict[str, Any]]]:
    if isinstance(content, str):
        return [content], []
    if content is None:
        return [], []
    if not isinstance(content, list):
        raise InvalidRequestError("message content must be a string or list", param=param)

    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            raise InvalidRequestError(
                f"{param}[{index}] must be an object",
                param=f"{param}[{index}]",
            )
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"}:
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
            else:
                raise InvalidRequestError(
                    f"{param}[{index}].text must be a string",
                    param=f"{param}[{index}].text",
                )
        elif part_type in {"input_image", "image_url"}:
            images.append(_parse_image(part, f"{param}[{index}]"))
        else:
            raise InvalidRequestError(
                f"Unsupported message content type: {part_type!r}",
                param=f"{param}[{index}].type",
                code="unsupported_content_part",
            )
    return texts, images


def _parse_image(item: dict[str, Any], param: str) -> dict[str, Any]:
    raw_url = item.get("image_url") or item.get("url")
    if isinstance(raw_url, dict):
        raw_url = raw_url.get("url")
    if not isinstance(raw_url, str) or not raw_url:
        if item.get("file_id") or item.get("fileId"):
            raise InvalidRequestError(
                "Codex app-server adapter does not resolve OpenAI file IDs; provide image_url",
                param=param,
                code="unsupported_file_reference",
            )
        raise InvalidRequestError("image_url is required", param=param)

    mapped: dict[str, Any] = {"type": "image", "url": raw_url}
    detail = item.get("detail")
    if isinstance(detail, str):
        mapped["detail"] = detail
    return mapped


def _stringify_tool_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    return repr(value)


def _text_input(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "textElements": []}


def _input_message(text: str) -> dict[str, Any]:
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "status": "completed",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _stored_input_item(item: dict[str, Any]) -> dict[str, Any]:
    stored = dict(item)
    stored.setdefault("id", f"item_{uuid.uuid4().hex}")
    if stored.get("type") == "message" or isinstance(stored.get("role"), str):
        stored.setdefault("type", "message")
        stored.setdefault("status", "completed")
    return stored


def _join_nonempty(parts: list[str]) -> str | None:
    value = "\n\n".join(part.strip() for part in parts if part and part.strip()).strip()
    return value or None
