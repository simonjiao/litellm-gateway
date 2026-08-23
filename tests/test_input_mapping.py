from __future__ import annotations

import pytest

from codex_responses_adapter.errors import InvalidRequestError
from codex_responses_adapter.input_mapping import map_responses_input


def test_maps_string_input() -> None:
    mapped = map_responses_input("inspect the repository", "be precise")
    assert mapped.user_inputs == [
        {"type": "text", "text": "inspect the repository", "textElements": []}
    ]
    assert mapped.developer_instructions == "be precise"


def test_maps_message_list_and_images() -> None:
    mapped = map_responses_input(
        [
            {"role": "system", "content": "follow repository instructions"},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "review this image"},
                    {"type": "input_image", "image_url": "https://example.test/a.png"},
                ],
            },
        ],
        None,
    )
    assert mapped.developer_instructions == "follow repository instructions"
    assert mapped.user_inputs[0]["type"] == "text"
    assert mapped.user_inputs[1] == {
        "type": "image",
        "url": "https://example.test/a.png",
    }


def test_rejects_unknown_item_type() -> None:
    with pytest.raises(InvalidRequestError):
        map_responses_input([{"type": "file_search_call"}], None)
