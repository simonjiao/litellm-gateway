from __future__ import annotations

import re

_SANDBOX_URI = re.compile(
    r"sandbox:/workspace/outputs/([A-Za-z0-9_-]{1,128})/([^\s<>\"'\]]+)"
)


def sandbox_candidates(text: str, assistant_message_id: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for match in _SANDBOX_URI.finditer(text):
        if match.group(1) != assistant_message_id:
            continue
        relative = match.group(2).rstrip(".,;:!?)}")
        parts = relative.split("/")
        if (
            relative
            and not relative.startswith("/")
            and all(part not in {"", ".", ".."} for part in parts)
            and relative not in seen
        ):
            seen.add(relative)
            candidates.append(relative)
    return candidates
