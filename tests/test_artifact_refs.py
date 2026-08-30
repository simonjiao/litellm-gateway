from sandbox_api.artifact_refs import sandbox_candidates


def test_sandbox_candidates_parse_markdown_without_joining_link_target() -> None:
    assistant_id = "assistant-message"
    uri = f"sandbox:/workspace/outputs/{assistant_id}/artifact-e2e.txt"

    assert sandbox_candidates(f"[{uri}]({uri})", assistant_id) == ["artifact-e2e.txt"]
    assert sandbox_candidates(f"[download]({uri})", assistant_id) == ["artifact-e2e.txt"]


def test_sandbox_candidates_enforce_message_and_relative_path() -> None:
    assistant_id = "assistant-message"
    prefix = f"sandbox:/workspace/outputs/{assistant_id}"
    text = f"{prefix}/nested/result.txt {prefix}/../secret.txt"

    assert sandbox_candidates(text, assistant_id) == ["nested/result.txt"]
    assert sandbox_candidates(text, "other-message") == []
