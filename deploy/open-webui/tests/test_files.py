"""Run inside the derived WebUI image with a disposable DATA_DIR."""

from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from agent_open_webui import database, files, router, workspace
from fastapi import HTTPException
from open_webui.models.chats import ChatForm, Chats
from open_webui.models.files import Files
from sqlalchemy.exc import IntegrityError
from starlette.requests import ClientDisconnect, Request


class FileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.create_tables()
        self.owner = SimpleNamespace(id="test-owner", role="admin")
        self.artifact_id = "artifact_" + uuid.uuid4().hex
        self.parts = []
        self.descriptor = {
            "artifact_id": self.artifact_id,
            "owner_id": self.owner.id,
            "filename": "data.csv",
            "media_type": "text/csv",
            "created_at": 1,
        }

    def request(self, chunks, size, *, disconnect=False):
        chunks = iter(chunks)

        async def receive():
            chunk = next(chunks, None)
            if chunk is None:
                return (
                    {"type": "http.disconnect"}
                    if disconnect
                    else {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }
                )
            return {"type": "http.request", "body": chunk, "more_body": True}

        return Request(
            {
                "type": "http",
                "headers": [
                    (b"x-file-name", b"data.csv"),
                    (b"x-file-type", b"text/csv"),
                    (b"x-file-size", str(size).encode()),
                ],
            },
            receive=receive,
        )

    async def upload(self, request):
        digest = hashlib.sha256()

        async def put(url, *, content, **kwargs):
            self.assertEqual(url, "http://artifact.test/upload")
            async for chunk in content:
                self.assertLessEqual(len(chunk), 64 * 1024)
                self.parts.append(len(chunk))
                digest.update(chunk)
            self.descriptor.update(size=sum(self.parts), sha256=digest.hexdigest())
            return httpx.Response(200, json=self.descriptor, request=httpx.Request("PUT", url))

        client = AsyncMock()
        client.__aenter__.return_value = client
        client.put.side_effect = put
        target = {
            "artifact_id": self.artifact_id,
            "url": "http://artifact.test/upload",
            "token": "test",
        }
        with (
            patch.object(files, "SETTINGS", replace(files.SETTINGS, enabled=True)),
            patch.object(files, "_artifact_request", AsyncMock(return_value=target)),
            patch.object(files.httpx, "AsyncClient", return_value=client),
            patch.object(request, "form", side_effect=AssertionError("must not parse multipart")),
        ):
            return await files.upload_file(request, self.owner)

    async def test_upload_streams_and_commits_metadata_and_hash_without_path(self):
        content = b"a,1\n" * 100000
        result = await self.upload(self.request([content[:190000], content[190000:]], len(content)))
        self.assertGreater(len(self.parts), 2)
        self.assertEqual(result["hash"], hashlib.sha256(content).hexdigest())
        self.assertIsNone(result["path"])
        self.assertEqual(result["data"], {"status": "completed"})
        binding = await database.get_file_artifact(result["id"])
        self.assertEqual(binding["artifact_id"], self.artifact_id)
        self.assertEqual(json.loads(binding["descriptor_json"])["size"], len(content))

    async def test_size_mismatch_and_disconnect_do_not_register_a_file(self):
        for size in (2, 4):
            with self.subTest(size=size), self.assertRaises(HTTPException) as error:
                await self.upload(self.request([b"abc"], size))
            self.assertEqual(error.exception.status_code, 422)
            self.assertIsNone(await database.get_file_by_artifact(self.artifact_id))
        with self.assertRaises(ClientDisconnect):
            await self.upload(self.request([b"abc"], 3, disconnect=True))
        self.assertIsNone(await database.get_file_by_artifact(self.artifact_id))

    async def test_metadata_failure_is_not_reported_as_upload_success(self):
        with patch.object(
            files, "register_artifact_file", AsyncMock(side_effect=RuntimeError("db"))
        ):
            with self.assertRaises(HTTPException) as error:
                await self.upload(self.request([b"abc"], 3))
        self.assertEqual(error.exception.status_code, 503)

    async def test_native_file_and_mapping_rollback_together(self):
        original = await self.upload(self.request([b"abc"], 3))
        second_id = str(uuid.uuid4())
        with self.assertRaises(IntegrityError):
            await database.register_artifact_file(second_id, self.owner.id, self.descriptor)
        self.assertIsNone(await Files.get_file_by_id(second_id))
        self.assertIsNone(await database.get_file_artifact(second_id))
        replay = await database.register_artifact_file(
            original["id"], self.owner.id, self.descriptor
        )
        self.assertEqual(replay["id"], original["id"])

    async def test_download_check_never_exposes_transfer_credentials(self):
        target = {"url": "http://private/transfer", "token": "secret"}
        with patch.object(router, "artifact_download_target", AsyncMock(return_value=target)):
            result = await router.download_artifact(self.artifact_id, self.owner, check=True)
        self.assertEqual(
            json.loads(result.body),
            {
                "url": f"/api/agent/artifacts/{self.artifact_id}/download",
            },
        )
        self.assertEqual(result.headers["cache-control"], "no-store")

    async def test_concurrent_generated_files_survive_reload_and_retry(self):
        message_id = str(uuid.uuid4())
        chat = await Chats.insert_new_chat(
            str(uuid.uuid4()),
            self.owner.id,
            ChatForm(
                chat={
                    "history": {
                        "currentId": message_id,
                        "messages": {
                            message_id: {
                                "id": message_id,
                                "role": "assistant",
                                "content": "done",
                                "parentId": None,
                                "childrenIds": [],
                            }
                        },
                    },
                }
            ),
        )
        intents = []
        for index in range(2):
            artifact_id = "artifact_" + uuid.uuid4().hex
            intent_id = await database.create_publish_intent(
                chat_id=chat.id,
                owner_user_id=self.owner.id,
                workspace_id="workspace-test",
                assistant_message_id=message_id,
                response_id="response-test",
                output_relative_path=f"result-{index}.csv",
            )
            await database.update_publish_intent(
                intent_id,
                artifact_id=artifact_id,
                state="uploaded",
                descriptor_json=json.dumps(
                    {**self.descriptor, "artifact_id": artifact_id, "size": 3, "sha256": "0" * 64}
                ),
            )
            intents.append(await database.get_publish_intent(intent_id))
        emitter = AsyncMock()
        with patch("open_webui.socket.main.get_event_emitter", AsyncMock(return_value=emitter)):
            await asyncio.gather(*(workspace._bind_published(intent) for intent in intents))
            await workspace._bind_published(intents[0])
        message = await Chats.get_message_by_id_and_message_id(chat.id, message_id)
        self.assertEqual(len(message["files"]), 2)
        self.assertEqual(len(emitter.call_args.args[0]["data"]["files"]), 2)
        for intent in intents:
            self.assertIsNotNone(await Files.get_file_by_id(intent["artifact_id"]))
            self.assertEqual((await database.get_publish_intent(intent["id"]))["state"], "ready")

    async def test_download_streams_and_closes_upstream(self):
        consumed = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for chunk in (b"a", b"b"):
                    consumed.append(chunk)
                    yield chunk

            async def aclose(self):
                consumed.append("closed")

        upstream = httpx.Response(
            200,
            stream=Stream(),
            headers={
                "Content-Disposition": 'attachment; filename="data.csv"',
                "Content-Length": "2",
            },
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: upstream))
        with patch.object(router.httpx, "AsyncClient", return_value=client):
            response = await router._proxy_download(
                {"url": "http://artifact.test/file", "token": "t"}
            )
        self.assertEqual(consumed, [])
        self.assertEqual(b"".join([part async for part in response.body_iterator]), b"ab")
        self.assertTrue(client.is_closed)
        self.assertEqual(consumed, [b"a", b"b", "closed"])
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="data.csv"')

    async def test_upstream_error_closes_connection_and_returns_page_safe_error(self):
        upstream = httpx.Response(503)
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: upstream))
        with patch.object(router.httpx, "AsyncClient", return_value=client):
            with self.assertRaises(HTTPException) as error:
                await router._proxy_download({"url": "http://artifact.test/file", "token": "t"})
        self.assertEqual(error.exception.status_code, 502)
        self.assertTrue(client.is_closed)
        self.assertTrue(upstream.is_closed)

    async def test_pending_failed_and_unknown_chat_downloads(self):
        for state, code in (("pending", 409), ("failed", 410)):
            with (
                patch.object(
                    router.Chats, "get_chat_by_id_for_user", AsyncMock(return_value=object())
                ),
                patch.object(
                    router, "get_response_binding", AsyncMock(return_value={"response_id": "r"})
                ),
                patch.object(
                    router,
                    "get_candidate_intent",
                    AsyncMock(
                        return_value={
                            "id": "intent",
                            "response_id": "r",
                            "state": state,
                        }
                    ),
                ),
                patch.object(router, "_schedule_intent"),
                self.assertRaises(HTTPException) as error,
            ):
                await router.download_candidate(
                    "chat", "message", "outputs/message/result.csv", self.owner, True
                )
            self.assertEqual(error.exception.status_code, code)
        with (
            patch.object(router.Chats, "get_chat_by_id_for_user", AsyncMock(return_value=None)),
            self.assertRaises(HTTPException) as error,
        ):
            await router.download_candidate(
                "chat", "message", "outputs/message/result.csv", self.owner, True
            )
        self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
