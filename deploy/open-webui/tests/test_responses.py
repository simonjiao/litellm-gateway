from __future__ import annotations

import asyncio
import json
import unittest
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agent_open_webui import database, responses, workspace
from fastapi import HTTPException
from open_webui.models.chats import ChatForm, Chats


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = patch.object(responses, "SETTINGS", replace(responses.SETTINGS, enabled=True))
        self.config.start()
        self.metadata = {"chat_id": str(uuid.uuid4()), "message_id": str(uuid.uuid4())}
        self.response_id = "resp_gateway_encoded_id"
        self.lines = [
            b": keepalive\n\n",
            b"event: response.created\n",
            b"data: "
            + json.dumps(
                {"type": "response.created", "response": {"id": self.response_id}}
            ).encode()
            + b"\n\n",
            b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n',
            b"data: [DONE]\n\n",
        ]

        async def content():
            for line in self.lines:
                yield line

        self.upstream = SimpleNamespace(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            content=content(),
            close=Mock(),
        )
        self.cancelled = SimpleNamespace(raise_for_status=Mock(), close=Mock())
        self.session = SimpleNamespace(
            request=AsyncMock(side_effect=[self.upstream, self.cancelled])
        )
        self.kwargs = {
            "method": "POST",
            "url": "http://gateway.test/v1/responses",
            "headers": {"Authorization": "Bearer fixture"},
            "data": "{}",
        }

    async def asyncTearDown(self):
        responses.finish_response(self.metadata)
        self.config.stop()

    async def test_stop_before_headers_keeps_request_alive_then_cancels_the_accepted_response(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed(**kwargs):
            if kwargs["url"].endswith("/cancel"):
                return self.cancelled
            started.set()
            await release.wait()
            return self.upstream

        self.session.request.side_effect = delayed
        task = asyncio.create_task(
            responses.request_response(self.session, self.metadata, True, **self.kwargs)
        )
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        stop = asyncio.create_task(responses.cancel_response(self.metadata))
        await asyncio.sleep(0)
        self.assertFalse(stop.done())
        release.set()
        await asyncio.wait_for(stop, 1)
        call = self.session.request.call_args.kwargs
        self.assertEqual(call["url"], f"http://gateway.test/v1/responses/{self.response_id}/cancel")
        self.assertEqual(call["headers"], self.kwargs["headers"])
        self.assertNotIn("data", call)
        self.upstream.close.assert_called_once()

    async def test_prefetch_preserves_every_stream_line_and_normal_finish_does_not_cancel(self):
        result = await responses.request_response(self.session, self.metadata, True, **self.kwargs)
        received = [line async for line in responses.stream_response(result.content, self.metadata)]
        self.assertEqual(received, self.lines)
        responses.finish_response(self.metadata)
        self.assertEqual(self.session.request.await_count, 1)
        self.assertNotIn(
            (self.metadata["chat_id"], self.metadata["message_id"]), responses._requests
        )

    async def test_stop_after_headers_uses_captured_id_even_if_stream_is_closed(self):
        await responses.request_response(self.session, self.metadata, True, **self.kwargs)
        await self.upstream.content.aclose()
        await responses.cancel_response(self.metadata)
        self.assertTrue(self.session.request.call_args.kwargs["url"].endswith("/cancel"))
        self.cancelled.raise_for_status.assert_called_once()

    async def test_rejected_request_has_no_response_to_cancel(self):
        self.upstream.status = 400
        await responses.request_response(self.session, self.metadata, True, **self.kwargs)
        await responses.cancel_response(self.metadata)
        self.assertEqual(self.session.request.await_count, 1)

    async def test_background_tasks_are_not_registered_as_the_main_response(self):
        metadata = {**self.metadata, "task": "title_generation"}
        await responses.request_response(self.session, metadata, True, **self.kwargs)
        await responses.cancel_response(metadata)
        self.assertEqual(self.session.request.await_count, 1)
        self.assertNotIn(
            (self.metadata["chat_id"], self.metadata["message_id"]), responses._requests
        )


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.create_tables()
        self.chat_id = str(uuid.uuid4())
        self.owner = SimpleNamespace(id="response-test-owner", role="admin")
        self.ids = [str(uuid.uuid4()) for _ in range(4)]
        messages = {}
        for index, message_id in enumerate(self.ids):
            messages[message_id] = {
                "id": message_id,
                "role": "user" if index % 2 == 0 else "assistant",
                "parentId": self.ids[index - 1] if index else None,
                "childrenIds": self.ids[index + 1 : index + 2],
                "content": "prompt" if index % 2 == 0 else "",
                "done": True,
            }
        messages[self.ids[3]]["error"] = {"content": "Previous Response binding is not ready"}
        await Chats.insert_new_chat(
            self.chat_id,
            self.owner.id,
            ChatForm(
                chat={
                    "history": {"currentId": self.ids[-1], "messages": messages},
                }
            ),
        )
        self.binding = dict(
            chat_id=self.chat_id,
            assistant_message_id=self.ids[1],
            response_id="resp_" + uuid.uuid4().hex,
            workspace_id="workspace-test",
            owner_user_id=self.owner.id,
        )

    async def test_continue_past_empty_error_uses_nearest_accepted_ancestor(self):
        await database.put_response_binding(**self.binding)
        with patch.object(workspace, "_await_response_binding", AsyncMock()) as wait:
            binding, pending = await workspace._previous_response_binding(self.chat_id, self.ids[3])
        self.assertEqual(binding["response_id"], self.binding["response_id"])
        self.assertEqual(pending, 2)
        wait.assert_not_awaited()

    async def test_initial_rejection_can_start_a_new_attempt(self):
        await Chats.upsert_message_to_chat_by_id_and_message_id(
            self.chat_id, self.ids[1], {"error": {"content": "upstream rejected"}}
        )
        binding, pending = await workspace._previous_response_binding(self.chat_id, self.ids[1])
        self.assertIsNone(binding)
        self.assertEqual(pending, 2)

    async def test_continuation_sends_only_input_after_the_bound_response(self):
        await database.put_response_binding(**self.binding)
        await database.insert_workspace(self.chat_id, self.binding["workspace_id"])
        inputs = [
            {"role": "user", "content": "already executed"},
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": "rejected request"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "new request"},
        ]
        new_user, new_assistant = str(uuid.uuid4()), str(uuid.uuid4())
        await Chats.upsert_message_to_chat_by_id_and_message_id(
            self.chat_id,
            new_user,
            {"role": "user", "parentId": self.ids[3], "content": "new request"},
        )
        await Chats.upsert_message_to_chat_by_id_and_message_id(
            self.chat_id,
            new_assistant,
            {"role": "assistant", "parentId": new_user, "content": "", "done": False},
        )
        with (
            patch.object(workspace, "SETTINGS", replace(workspace.SETTINGS, enabled=True)),
            patch.object(workspace, "_await_capture_barriers", AsyncMock()),
            patch.object(workspace, "_checkout_grant", AsyncMock(return_value=("checkout", []))),
            patch.object(workspace, "_grant", return_value="grant"),
        ):
            for parent_id, start, user_id, assistant_id in (
                (self.ids[1], 4, self.ids[2], self.ids[3]),
                (self.ids[3], 2, new_user, new_assistant),
            ):
                metadata = {
                    "chat_id": self.chat_id,
                    "user_message_id": user_id,
                    "assistant_message_id": assistant_id,
                    "user_message": {"parentId": parent_id},
                }
                payload = await workspace.inject_workspace_context(
                    None, self.owner, metadata, {"input": inputs.copy()}
                )
                self.assertEqual(payload["input"], inputs[start:])
                self.assertEqual(payload["previous_response_id"], self.binding["response_id"])

    async def test_running_or_nonempty_unbound_response_cannot_be_skipped(self):
        for state in ({"done": False}, {"done": True, "content": "partial"}):
            with self.subTest(state=state):
                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    self.chat_id, self.ids[1], state
                )
                with patch.object(
                    workspace, "_await_response_binding", AsyncMock(return_value=None)
                ):
                    with self.assertRaises(HTTPException) as error:
                        await workspace._previous_response_binding(self.chat_id, self.ids[1])
                self.assertEqual(error.exception.status_code, 409)

    async def test_binding_cannot_be_taken_from_another_chat(self):
        await database.put_response_binding(**self.binding)
        with self.assertRaises(HTTPException) as error:
            await workspace._previous_response_binding(str(uuid.uuid4()), self.ids[1])
        self.assertEqual(error.exception.status_code, 404)

    async def test_native_task_limit_is_removed_but_explicit_chat_limit_is_preserved(self):
        with patch.object(workspace, "SETTINGS", replace(workspace.SETTINGS, enabled=True)):
            task = await workspace.inject_workspace_context(
                None, self.owner, {"task": "title_generation"}, {"max_output_tokens": 1000}
            )
            chat = await workspace.inject_workspace_context(
                None, self.owner, {}, {"max_output_tokens": 1000}
            )
        self.assertNotIn("max_output_tokens", task)
        self.assertEqual(chat["max_output_tokens"], 1000)
