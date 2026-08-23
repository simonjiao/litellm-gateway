from __future__ import annotations

from codex_responses_adapter.errors import UpstreamProtocolError


def test_upstream_details_are_not_returned_to_clients() -> None:
    error = UpstreamProtocolError("upstream failed", details={"secret": "do-not-expose"})
    assert "details" not in error.envelope()["error"]
