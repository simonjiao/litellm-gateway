from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sandbox_worker.codex_protocol import CodexAppServerSession
from sandbox_worker.settings import WorkerSettings


@pytest.mark.asyncio
async def test_jsonl_session_round_trip() -> None:
    fake = Path(__file__).with_name("fake_app_server.py")
    settings = WorkerSettings(
        codex_command=f"{sys.executable} {fake}",
        codex_workdir=fake.parent,
        request_timeout_seconds=5,
    )
    async with CodexAppServerSession(settings) as session:
        thread_result = await session.request("thread/start", {"cwd": str(fake.parent)})
        thread_id = thread_result["thread"]["id"]
        turn_result = await session.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "hello", "textElements": []}],
            },
        )
        turn_id = turn_result["turn"]["id"]
        methods: list[str] = []
        while "turn/completed" not in methods:
            notification = await session.next_notification()
            methods.append(notification["method"])
        assert turn_id.startswith("turn_")
        assert "item/agentMessage/delta" in methods


@pytest.mark.asyncio
async def test_jsonl_session_does_not_leak_worker_control_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_WORKER_API_KEY", "worker-control-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://egress-proxy:3128")
    fake = Path(__file__).with_name("fake_app_server.py")
    settings = WorkerSettings(
        codex_command=f"{sys.executable} {fake}",
        codex_workdir=fake.parent,
        request_timeout_seconds=5,
    )
    async with CodexAppServerSession(settings) as session:
        environment = await session.request("test/environment", {})

    assert environment["sandbox_worker_api_key"] is None
    assert environment["http_proxy"] == "http://egress-proxy:3128"
