from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx

from sandbox_manager.models import OperationInfo, SandboxInfo, WorkspaceInfo
from sandbox_worker.models import AgentEvent

from .errors import InvalidRequestError, ResponseConflictError, UpstreamProtocolError
from .events import ResponsesEventBuilder, mcp_app_side_event
from .input_mapping import MappedInput, map_responses_input
from .mcp_apps import (
    AppInteraction,
    McpAppsState,
    McpAppToolCallRequest,
    ResolveInteractionRequest,
)
from .models import CreateResponseRequest, ResponseRecord
from .sandbox import SandboxClient, SandboxUnavailableError
from .settings import Settings
from .store import ActiveExecution, ResponseStore

logger = logging.getLogger(__name__)


class _TerminalAgentExecutionError(UpstreamProtocolError):
    pass


@dataclass(slots=True)
class PreparedExecution:
    record: ResponseRecord
    active: ActiveExecution
    builder: ResponsesEventBuilder
    publish_grant: str | None = None


@dataclass(frozen=True, slots=True)
class _WorkspaceContext:
    grant: str | None
    checkout_grant: str | None
    publish_grant: str | None
    public_metadata: dict[str, Any]


class CodexResponsesService:
    def __init__(
        self,
        settings: Settings,
        store: ResponseStore,
        mcp_apps: McpAppsState,
        sandbox_client: SandboxClient,
    ) -> None:
        self._settings = settings
        self._store = store
        self._mcp_apps = mcp_apps
        self._sandbox = sandbox_client
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_executions)

    async def create_non_streaming(self, request: CreateResponseRequest) -> dict[str, Any]:
        prepared, _ = await self._start(request, subscribe=False)
        # The HTTP request is only a waiter. Cancelling it must not cancel the Agent
        # execution owned by its Sandbox Worker.
        await asyncio.shield(prepared.active.terminal.wait())
        if prepared.active.driver_error is not None and not prepared.active.cancel_requested:
            raise prepared.active.driver_error
        return prepared.record.to_response()

    async def create_streaming(
        self,
        request: CreateResponseRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        """Create the turn before HTTP headers and return an event subscription."""

        prepared, queue = await self._start(request, subscribe=True)
        assert queue is not None
        return self._stream_subscription(prepared.active, queue)

    async def _stream_subscription(
        self,
        active: ActiveExecution,
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            # An SSE disconnect removes one subscriber only. The independent driver
            # keeps consuming Worker events and updating Response state.
            await active.unsubscribe(queue)

    async def retrieve(self, response_id: str) -> dict[str, Any]:
        return (await self._store.get(response_id)).to_response()

    async def cancel(self, response_id: str) -> dict[str, Any]:
        record = await self._store.get(response_id)
        await self._mcp_apps.cancel_response_interactions(response_id)
        active = await self._store.get_active(response_id)
        if active is None or active.cancel_requested:
            return record.to_response()
        active.cancel_requested = True
        try:
            await self._sandbox.rpc(
                active.sandbox_id,
                "turn/interrupt",
                {"threadId": active.thread_id, "turnId": active.turn_id},
            )
        except Exception:
            logger.warning(
                "Codex interrupt failed for %s; terminating sandbox %s",
                response_id,
                active.sandbox_id,
                exc_info=True,
            )
            with suppress(Exception):
                await self._sandbox.terminate_sandbox(active.sandbox_id)
        return record.to_response()

    async def delete(self, response_id: str) -> dict[str, Any]:
        if await self._store.get_active(response_id) is not None:
            raise ResponseConflictError(
                "An in-progress response must be cancelled before deletion."
            )
        record = await self._store.delete(response_id)
        await self._mcp_apps.delete_response(response_id)
        return {"id": record.id, "object": "response.deleted", "deleted": True}

    async def list_input_items(self, response_id: str) -> dict[str, Any]:
        record = await self._store.get(response_id)
        data = record.input_items
        first_id = _item_id(data[0]) if data else None
        last_id = _item_id(data[-1]) if data else None
        return {
            "object": "list",
            "data": data,
            "first_id": first_id,
            "last_id": last_id,
            "has_more": False,
        }

    async def create_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        return await self._sandbox.create_workspace(workspace_id, grant)

    async def release_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        return await self._sandbox.release_workspace(workspace_id, grant)

    async def publish_artifact(self, response_id: str, grant: str) -> OperationInfo:
        record = await self._store.get(response_id)
        if record.workspace_id is None or record.sandbox_id is None:
            raise ResponseConflictError(
                "The response has no active Workspace.",
                code="workspace_unavailable",
            )
        operation = await self._sandbox.create_operation(grant)
        operation = await self._wait_publish_capture(operation)
        if (
            operation.operation != "publish"
            or operation.workspace_id != record.workspace_id
            or operation.sandbox_id != record.sandbox_id
        ):
            raise UpstreamProtocolError("Workspace artifact publish binding changed")
        return operation

    async def inspect_artifact_operation(self, operation_id: str) -> OperationInfo:
        operation = await self._sandbox.inspect_operation(operation_id)
        if operation.operation != "publish":
            raise ResponseConflictError("The operation is not an Artifact publish operation.")
        return operation

    async def app_state(self, response_id: str) -> dict[str, Any]:
        await self._store.get(response_id)
        state = await self._mcp_apps.snapshot(response_id)
        interactions = state.get("interactions")
        if isinstance(interactions, list):
            for interaction in interactions:
                if isinstance(interaction, dict) and isinstance(interaction.get("id"), str):
                    interaction["resolve_url"] = self._interaction_resolve_url(interaction["id"])
        return state

    async def app_events(
        self,
        response_id: str,
        *,
        after: int,
    ) -> AsyncIterator[dict[str, Any] | None]:
        await self._store.get(response_id)
        cursor = after
        while True:
            events, closed = await self._mcp_apps.wait_for_events(
                response_id,
                after=cursor,
                timeout_seconds=self._settings.mcp_apps_event_keepalive_seconds,
            )
            if not events:
                if closed:
                    return
                yield None
                continue
            for event in events:
                cursor = max(cursor, int(event.get("sequence_number", cursor)))
                yield event
            if closed:
                return

    async def get_interaction(self, interaction_id: str) -> dict[str, Any]:
        interaction = await self._mcp_apps.get_interaction(interaction_id)
        return self._public_interaction(interaction)

    async def resolve_interaction(
        self,
        interaction_id: str,
        request: ResolveInteractionRequest,
    ) -> dict[str, Any]:
        interaction = await self._mcp_apps.resolve_interaction(interaction_id, request)
        await self._mcp_apps.publish(
            interaction.response_id,
            "mcp_app.elicitation.resolved",
            {"interaction": self._public_interaction(interaction)},
        )
        return self._public_interaction(interaction)

    async def read_mcp_resource(
        self,
        response_id: str,
        *,
        server: str,
        uri: str,
        origin_call_id: str | None,
        connector_id: str | None,
    ) -> dict[str, Any]:
        record = await self._store.get(response_id)
        await self._validate_mcp_app_access(
            record,
            server,
            origin_call_id,
            resource_uri=uri,
            connector_id=connector_id,
        )
        sandbox_id = await self._renew_available_sandbox(record)
        params: dict[str, Any] = {
            "threadId": self._thread_id(record),
            "server": server,
            "uri": uri,
            "originCallId": origin_call_id,
        }
        if connector_id:
            params["connectorId"] = connector_id
        result = await self._sandbox.rpc(sandbox_id, "mcpServer/resource/read", params)
        if not isinstance(result, dict):
            raise UpstreamProtocolError(
                "Codex app-server returned an invalid MCP resource response",
                details=result,
            )
        return result

    async def call_mcp_tool(
        self,
        response_id: str,
        request: McpAppToolCallRequest,
    ) -> dict[str, Any]:
        record = await self._store.get(response_id)
        await self._validate_mcp_app_access(
            record,
            request.server,
            request.origin_call_id,
            tool=request.tool,
        )
        params: dict[str, Any] = {
            "threadId": self._thread_id(record),
            "server": request.server,
            "tool": request.tool,
        }
        if request.arguments is not None:
            if not isinstance(request.arguments, dict):
                raise InvalidRequestError(
                    "MCP tool arguments must be an object.",
                    param="arguments",
                    code="invalid_mcp_tool_arguments",
                )
            params["arguments"] = request.arguments
        if request.meta is not None:
            params["_meta"] = request.meta
        sandbox_id = await self._renew_available_sandbox(record)
        result = await self._sandbox.rpc(sandbox_id, "mcpServer/tool/call", params)
        if not isinstance(result, dict):
            raise UpstreamProtocolError(
                "Codex app-server returned an invalid MCP tool response",
                details=result,
            )
        return result

    async def shutdown(self) -> None:
        active = await self._store.active_executions()
        tasks = [item.task for item in active if item.task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        await self._sandbox.aclose()

    async def _start(
        self,
        request: CreateResponseRequest,
        *,
        subscribe: bool,
    ) -> tuple[
        PreparedExecution,
        asyncio.Queue[dict[str, Any] | None] | None,
    ]:
        self._validate_request(request)
        await self._semaphore.acquire()
        created_sandbox_id: str | None = None
        try:
            workspace_context = _workspace_context(request.metadata)
            mapped = map_responses_input(request.input, request.instructions)
            record = ResponseRecord.create(
                request.model_copy(update={"metadata": workspace_context.public_metadata}),
                mapped.input_items,
                mcp_apps_base_url=self._settings.mcp_apps_public_base_url,
            )

            previous: ResponseRecord | None = None
            if request.previous_response_id:
                previous = await self._store.get(request.previous_response_id)
                if await self._store.get_active(previous.id) is not None:
                    raise ResponseConflictError(
                        "previous_response_id is still in progress.",
                        code="previous_response_in_progress",
                    )
                if not previous.sandbox_id:
                    raise InvalidRequestError(
                        "previous_response_id has no Sandbox mapping",
                        param="previous_response_id",
                    )
                if previous.workspace_recoverable:
                    if previous.workspace_id is None or workspace_context.grant is None:
                        raise InvalidRequestError(
                            "A Workspace grant is required to continue this response.",
                            param="metadata",
                            code="workspace_grant_required",
                        )
                    authorized = await self._sandbox.authorize_workspace(
                        previous.workspace_id,
                        workspace_context.grant,
                    )
                    if authorized.id != previous.workspace_id:
                        raise InvalidRequestError(
                            "The Workspace grant does not match previous_response_id.",
                            param="previous_response_id",
                            code="workspace_mismatch",
                        )
                elif workspace_context.grant is not None:
                    raise InvalidRequestError(
                        "An ephemeral response cannot switch to a recoverable Workspace.",
                        param="previous_response_id",
                        code="workspace_mismatch",
                    )
                try:
                    sandbox = await self._sandbox.inspect_sandbox(previous.sandbox_id)
                    if sandbox.status != "running" or sandbox.worker is None:
                        raise SandboxUnavailableError("Sandbox is not running")
                    sandbox = await self._sandbox.renew_sandbox(previous.sandbox_id)
                except (SandboxUnavailableError, UpstreamProtocolError) as exc:
                    raise ResponseConflictError(
                        "The Sandbox for previous_response_id is unavailable.",
                        code="sandbox_unavailable",
                    ) from exc
                if sandbox.status != "running" or sandbox.worker is None:
                    raise ResponseConflictError(
                        "The Sandbox for previous_response_id is unavailable.",
                        code="sandbox_unavailable",
                    )
                if sandbox.workspace_id != previous.workspace_id:
                    raise ResponseConflictError(
                        "The Sandbox Workspace for previous_response_id changed.",
                        code="workspace_mismatch",
                    )
            else:
                sandbox = await self._sandbox.create_sandbox(workspace_context.grant)
                created_sandbox_id = sandbox.id
                if workspace_context.grant is not None and (
                    not sandbox.recoverable or sandbox.workspace_id is None
                ):
                    raise UpstreamProtocolError(
                        "Sandbox Manager did not attach the authorized Workspace"
                    )

            record.sandbox_id = sandbox.id
            record.workspace_id = sandbox.workspace_id
            record.workspace_recoverable = sandbox.recoverable
            await self._run_checkout_operation(sandbox, workspace_context.checkout_grant)
            thread_id = await self._start_or_continue_thread(sandbox.id, request, mapped, previous)
            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": mapped.user_inputs,
                "approvalPolicy": "never",
                "sandboxPolicy": {
                    "type": "externalSandbox",
                    "networkAccess": "restricted",
                },
            }
            if isinstance(request.reasoning, dict):
                effort = request.reasoning.get("effort")
                if isinstance(effort, str) and effort:
                    turn_params["effort"] = effort
                summary = request.reasoning.get("summary")
                if isinstance(summary, str) and summary:
                    turn_params["summary"] = summary
            if request.service_tier:
                turn_params["serviceTier"] = request.service_tier
            event_cursor = await self._sandbox.event_cursor(sandbox.id)
            turn_result = await self._sandbox.rpc(sandbox.id, "turn/start", turn_params)
            turn = turn_result.get("turn") if isinstance(turn_result, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str) or not turn_id:
                raise UpstreamProtocolError(
                    "Codex app-server turn/start response did not include turn.id",
                    details=turn_result,
                )

            record.thread_id = thread_id
            record.turn_id = turn_id
            active = ActiveExecution(
                response_id=record.id,
                sandbox_id=sandbox.id,
                thread_id=thread_id,
                turn_id=turn_id,
                event_cursor=event_cursor,
            )
            builder = ResponsesEventBuilder(record)
            prepared = PreparedExecution(
                record=record,
                active=active,
                builder=builder,
                publish_grant=workspace_context.publish_grant,
            )
            await self._store.put(record)
            await self._store.register_active(active)
            queue = await active.subscribe() if subscribe else None
            initial = builder.initial_events()
            await self._publish_response_events(prepared, initial)
            active.task = asyncio.create_task(
                self._drive(prepared), name=f"response-driver-{record.id}"
            )
            return prepared, queue
        except BaseException:
            if created_sandbox_id is not None:
                with suppress(Exception):
                    await self._sandbox.terminate_sandbox(created_sandbox_id)
            self._semaphore.release()
            raise

    async def _run_checkout_operation(
        self,
        sandbox: SandboxInfo,
        grant: str | None,
    ) -> None:
        if grant is None:
            return
        if sandbox.workspace_id is None:
            raise UpstreamProtocolError("Sandbox Manager did not return a Workspace binding")
        deadline = asyncio.get_running_loop().time() + self._settings.request_timeout_seconds
        operation = await self._sandbox.create_operation(grant)
        operation = await self._wait_operation(operation, deadline=deadline)
        if (
            operation.status != "succeeded"
            or operation.workspace_id != sandbox.workspace_id
            or operation.sandbox_id != sandbox.id
        ):
            raise UpstreamProtocolError(operation.error or "Workspace file checkout failed")

    async def _wait_operation(
        self,
        operation: OperationInfo,
        *,
        deadline: float | None = None,
    ) -> OperationInfo:
        effective_deadline = deadline or (
            asyncio.get_running_loop().time() + self._settings.request_timeout_seconds
        )
        while operation.status in {"pending", "running"}:
            if asyncio.get_running_loop().time() >= effective_deadline:
                raise UpstreamProtocolError("Workspace operation timed out")
            await asyncio.sleep(0.1)
            operation = await self._sandbox.inspect_operation(operation.id)
        return operation

    async def _wait_publish_capture(self, operation: OperationInfo) -> OperationInfo:
        deadline = asyncio.get_running_loop().time() + self._settings.request_timeout_seconds
        while operation.status in {"pending", "running"} and operation.phase not in {
            "captured",
            "uploading",
        }:
            if asyncio.get_running_loop().time() >= deadline:
                raise UpstreamProtocolError("Workspace publish capture timed out")
            await asyncio.sleep(0.1)
            operation = await self._sandbox.inspect_operation(operation.id)
        return operation

    async def _drive(self, prepared: PreparedExecution) -> None:
        active = prepared.active
        builder = prepared.builder
        lease_task = asyncio.create_task(
            self._maintain_sandbox_lease(active),
            name=f"sandbox-lease-{active.sandbox_id}",
        )
        reconnect_deadline = (
            asyncio.get_running_loop().time() + self._settings.request_timeout_seconds
        )
        try:
            while not builder.terminal:
                try:
                    async for event in self._sandbox.events(
                        active.sandbox_id, after=active.event_cursor
                    ):
                        if active.lease_error is not None:
                            raise active.lease_error
                        if event.id > active.event_cursor + 1:
                            raise _TerminalAgentExecutionError(
                                "Sandbox worker event history has a gap"
                            )
                        if event.type == "server_request":
                            await self._handle_agent_server_request(prepared, event)
                        elif event.type == "session_failed":
                            raise _TerminalAgentExecutionError(
                                str(event.data.get("message") or "Sandbox worker session failed")
                            )
                        else:
                            notification = event.data
                            if _belongs_to_execution(notification, active):
                                response_events = builder.consume(notification)
                                await self._publish_response_events(prepared, response_events)
                        active.event_cursor = event.id
                        if builder.terminal:
                            return
                    raise UpstreamProtocolError(
                        "Sandbox Worker event stream ended before the turn completed"
                    )
                except _TerminalAgentExecutionError:
                    raise
                except UpstreamProtocolError:
                    if active.lease_error is not None:
                        raise active.lease_error from None
                    if (
                        active.cancel_requested
                        or asyncio.get_running_loop().time() >= reconnect_deadline
                    ):
                        raise
                    sandbox = await self._sandbox.inspect_sandbox(active.sandbox_id)
                    if sandbox.status != "running" or sandbox.worker is None:
                        raise
                    await self._sandbox.renew_sandbox(active.sandbox_id)
                    await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            active.driver_error = exc
            if active.cancel_requested:
                events = builder.incomplete("cancelled")
            else:
                events = builder.fail(_safe_error_message(exc))
            await self._publish_response_events(prepared, events)
        finally:
            lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await lease_task
            await self._notify_terminal(prepared)
            await self._cleanup(prepared)

    async def _notify_terminal(self, prepared: PreparedExecution) -> None:
        if prepared.publish_grant is None or self._settings.publish_callback_url is None:
            return
        body = {
            "response_id": prepared.record.id,
            "status": prepared.record.status,
            "output_text": prepared.record.output_text,
            "grant": prepared.publish_grant,
        }
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(10, connect=3), trust_env=False
                ) as client:
                    response = await client.post(self._settings.publish_callback_url, json=body)
                response.raise_for_status()
                return
            except httpx.HTTPError:
                if attempt == 2:
                    logger.exception(
                        "Failed to deliver terminal Artifact event for %s",
                        prepared.record.id,
                    )
                    return
                await asyncio.sleep(0.25 * (2**attempt))

    async def _maintain_sandbox_lease(self, active: ActiveExecution) -> None:
        try:
            while True:
                await asyncio.sleep(self._settings.sandbox_lease_renew_interval_seconds)
                sandbox = await self._sandbox.renew_sandbox(active.sandbox_id)
                if sandbox.status != "running" or sandbox.worker is None:
                    raise SandboxUnavailableError(f"Sandbox '{active.sandbox_id}' is unavailable")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            active.lease_error = exc

    async def _handle_agent_server_request(
        self,
        prepared: PreparedExecution,
        event: AgentEvent,
    ) -> None:
        active = prepared.active
        request_id = event.data.get("request_id")
        method = event.data.get("method")
        params = event.data.get("params")
        if not isinstance(request_id, (int, str)) or not isinstance(method, str):
            raise UpstreamProtocolError(
                "Sandbox worker emitted an invalid server request", details=event.data
            )
        request_params = params if isinstance(params, dict) else {}
        if not _belongs_to_execution({"params": request_params}, active):
            await self._sandbox.resolve_server_request(
                active.sandbox_id,
                request_id,
                result=None,
                error={
                    "code": -32000,
                    "message": "Interactive request does not belong to the active turn",
                },
            )
            return
        try:
            result = await self._handle_server_request(prepared.record, method, request_params)
        except Exception as exc:
            await self._sandbox.resolve_server_request(
                active.sandbox_id,
                request_id,
                result=None,
                error={"code": -32000, "message": _safe_error_message(exc)},
            )
            return
        await self._sandbox.resolve_server_request(
            active.sandbox_id,
            request_id,
            result=result,
            error=None,
        )

    async def _handle_server_request(
        self,
        record: ResponseRecord,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._settings.mcp_apps_enabled:
            raise UpstreamProtocolError("MCP Apps support is disabled")
        if method != "mcpServer/elicitation/request":
            raise UpstreamProtocolError(f"Unsupported interactive app-server request: {method}")
        if not record.stream:
            return {"action": "cancel", "content": None, "_meta": None}

        interaction = await self._mcp_apps.create_interaction(
            response_id=record.id,
            method=method,
            params=params,
        )
        await self._mcp_apps.publish(
            record.id,
            "mcp_app.elicitation.requested",
            {"interaction": self._public_interaction(interaction)},
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(interaction.future),
                timeout=self._settings.mcp_apps_interaction_timeout_seconds,
            )
        except TimeoutError:
            interaction = await self._mcp_apps.expire_interaction(interaction.id)
            result = interaction.result or {
                "action": "cancel",
                "content": None,
                "_meta": None,
            }
            await self._mcp_apps.publish(
                record.id,
                "mcp_app.elicitation.expired",
                {"interaction": self._public_interaction(interaction)},
            )
        return result

    async def _start_or_continue_thread(
        self,
        execution_id: str,
        request: CreateResponseRequest,
        mapped: MappedInput,
        previous: ResponseRecord | None,
    ) -> str:
        common = self._thread_common(
            model=request.model,
            developer_instructions=mapped.developer_instructions,
            service_tier=request.service_tier,
        )
        if previous is not None:
            if self._settings.codex_ephemeral_threads:
                raise InvalidRequestError(
                    "previous_response_id is unavailable when ephemeral Codex threads are enabled",
                    param="previous_response_id",
                    code="ephemeral_thread_not_resumable",
                )
            if not previous.thread_id or not previous.turn_id:
                raise InvalidRequestError(
                    "previous_response_id does not reference a Codex thread turn",
                    param="previous_response_id",
                )
            result = await self._sandbox.rpc(
                execution_id,
                "thread/fork",
                {
                    "threadId": previous.thread_id,
                    "lastTurnId": previous.turn_id,
                    "ephemeral": False,
                    **common,
                },
            )
        else:
            result = await self._sandbox.rpc(
                execution_id,
                "thread/start",
                {**common, "ephemeral": self._settings.codex_ephemeral_threads},
            )
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            method = "thread/fork" if previous is not None else "thread/start"
            raise UpstreamProtocolError(
                f"Codex app-server {method} response did not include thread.id",
                details=result,
            )
        return thread_id

    def _thread_common(
        self,
        *,
        model: str,
        developer_instructions: str | None = None,
        service_tier: str | None = None,
    ) -> dict[str, Any]:
        common: dict[str, Any] = {
            "cwd": self._settings.agent_workspace,
            "approvalPolicy": "never",
            "model": self._settings.codex_model or model,
        }
        if service_tier:
            common["serviceTier"] = service_tier
        if developer_instructions:
            common["developerInstructions"] = developer_instructions
        return common

    async def _publish_response_events(
        self,
        prepared: PreparedExecution,
        events: list[dict[str, Any]],
    ) -> None:
        side_events: list[tuple[str, dict[str, Any]]] = []
        if self._settings.mcp_apps_enabled:
            for event in events:
                side = mcp_app_side_event(event)
                if side is None:
                    continue
                event_type, data = side
                item = data.get("item")
                meta = item.get("_meta") if isinstance(item, dict) else None
                descriptor = meta.get("mcp_app") if isinstance(meta, dict) else None
                if isinstance(item, dict) and isinstance(descriptor, dict):
                    await self._bind_app_session(prepared, item, descriptor)
                side_events.append((event_type, data))

        await prepared.active.publish(events)
        for event_type, data in side_events:
            await self._mcp_apps.publish(prepared.record.id, event_type, data)

    async def _bind_app_session(
        self,
        prepared: PreparedExecution,
        item: dict[str, Any],
        descriptor: dict[str, Any],
    ) -> None:
        origin_call_id = descriptor.get("call_id")
        if not isinstance(origin_call_id, str) or not origin_call_id:
            raise InvalidRequestError(
                "Codex emitted an invalid MCP App descriptor.",
                code="invalid_mcp_app_descriptor",
            )
        session = await self._mcp_apps.find_app_session(prepared.record.id, origin_call_id)
        if session is None:
            descriptor["allowed_tools"] = await self._load_app_allowed_tools(prepared, descriptor)
            session = await self._mcp_apps.register_app_session(prepared.record.id, descriptor)
        else:
            descriptor["allowed_tools"] = list(session.allowed_tools)
        prepared.record.register_mcp_app_target(origin_call_id, descriptor)
        if isinstance(item.get("id"), str):
            prepared.record.update_output_item(item)

    async def _load_app_allowed_tools(
        self,
        prepared: PreparedExecution,
        descriptor: dict[str, Any],
    ) -> list[str]:
        app_id = descriptor.get("app_id")
        if not isinstance(app_id, str) or not app_id:
            return []
        try:
            result = await self._sandbox.rpc(
                prepared.active.sandbox_id,
                "app/read",
                {
                    "appIds": [app_id],
                    "threadId": prepared.active.thread_id,
                    "includeTools": True,
                },
            )
        except Exception:
            logger.warning("Unable to resolve MCP App tool scope for %s", app_id, exc_info=True)
            return []
        apps = result.get("apps") if isinstance(result, dict) else None
        if not isinstance(apps, list):
            return []
        for app in apps:
            if not isinstance(app, dict) or app.get("id") != app_id:
                continue
            summaries = app.get("toolSummaries")
            if not isinstance(summaries, list):
                return []
            return list(
                dict.fromkeys(
                    summary["name"]
                    for summary in summaries
                    if isinstance(summary, dict)
                    and summary.get("isEnabled") is True
                    and isinstance(summary.get("name"), str)
                    and summary["name"]
                )
            )
        return []

    async def _cleanup(self, prepared: PreparedExecution) -> None:
        await self._store.unregister_active(prepared.record.id)
        await self._mcp_apps.cancel_response_interactions(prepared.record.id)
        if self._settings.mcp_apps_enabled:
            await self._mcp_apps.publish(
                prepared.record.id,
                "mcp_app.response.closed",
                {
                    "response_id": prepared.record.id,
                    "status": prepared.record.status,
                },
            )
        await self._mcp_apps.close_response(prepared.record.id)
        await prepared.active.finish()
        self._semaphore.release()

    def _public_interaction(self, interaction: AppInteraction) -> dict[str, Any]:
        public = interaction.public()
        public["resolve_url"] = self._interaction_resolve_url(interaction.id)
        return public

    def _interaction_resolve_url(self, interaction_id: str) -> str:
        return self._settings.public_url(f"/v1/mcp-apps/interactions/{interaction_id}/resolve")

    async def _validate_mcp_app_access(
        self,
        record: ResponseRecord,
        server: str,
        origin_call_id: str | None,
        *,
        resource_uri: str | None = None,
        connector_id: str | None = None,
        tool: str | None = None,
    ) -> None:
        if not origin_call_id:
            raise InvalidRequestError(
                "origin_call_id is required for MCP App access.",
                param="origin_call_id",
                code="invalid_mcp_app_origin",
            )
        session = await self._mcp_apps.get_app_session(record.id, origin_call_id)
        if session.server_id != server:
            raise InvalidRequestError(
                "MCP App call does not belong to this response.",
                param="origin_call_id",
                code="invalid_mcp_app_origin",
            )
        if resource_uri is not None and session.resource_uri != resource_uri:
            raise InvalidRequestError(
                "MCP App resource URI is outside the AppSession scope.",
                param="uri",
                code="invalid_mcp_app_resource",
            )
        target = record.mcp_app_targets.get(origin_call_id, {})
        if connector_id is not None and target.get("connector_id") != connector_id:
            raise InvalidRequestError(
                "MCP App connector is outside the AppSession scope.",
                param="connector_id",
                code="invalid_mcp_app_connector",
            )
        if tool is not None:
            if tool not in session.allowed_tools:
                raise InvalidRequestError(
                    "MCP App tool is outside the AppSession scope.",
                    param="tool",
                    code="mcp_app_tool_not_allowed",
                )

    async def _renew_available_sandbox(self, record: ResponseRecord) -> str:
        sandbox_id = self._sandbox_id(record)
        try:
            sandbox = await self._sandbox.inspect_sandbox(sandbox_id)
            if sandbox.status != "running" or sandbox.worker is None:
                raise SandboxUnavailableError(f"Sandbox '{sandbox_id}' is unavailable")
            sandbox = await self._sandbox.renew_sandbox(sandbox_id)
            if sandbox.status != "running" or sandbox.worker is None:
                raise SandboxUnavailableError(f"Sandbox '{sandbox_id}' is unavailable")
        except (SandboxUnavailableError, UpstreamProtocolError) as exc:
            raise ResponseConflictError(
                "The Sandbox is unavailable.",
                code="sandbox_unavailable",
            ) from exc
        return sandbox_id

    @staticmethod
    def _sandbox_id(record: ResponseRecord) -> str:
        if not record.sandbox_id:
            raise ResponseConflictError(
                "The Sandbox is unavailable.",
                code="sandbox_unavailable",
            )
        return record.sandbox_id

    @staticmethod
    def _thread_id(record: ResponseRecord) -> str:
        if not record.thread_id:
            raise ResponseConflictError(
                "The Codex thread is unavailable.",
                code="mcp_app_thread_unavailable",
            )
        return record.thread_id

    @staticmethod
    def _validate_request(request: CreateResponseRequest) -> None:
        if request.background:
            raise InvalidRequestError(
                "background=true is not supported by the Codex app-server adapter",
                param="background",
                code="unsupported_background_mode",
            )
        if request.tools:
            raise InvalidRequestError(
                "Client-supplied Responses tools are not mapped. "
                "MCP Apps must be configured in Codex app-server.",
                param="tools",
                code="unsupported_tools",
            )
        if request.max_output_tokens is not None:
            raise InvalidRequestError(
                "max_output_tokens is not enforced by Codex app-server "
                "and is not supported by this adapter",
                param="max_output_tokens",
                code="unsupported_max_output_tokens",
            )
        if request.store is False:
            raise InvalidRequestError(
                "store=false is not supported because response, thread, "
                "and MCP App mappings are required",
                param="store",
                code="unsupported_store_mode",
            )
        if isinstance(request.reasoning, dict):
            unsupported_reasoning = sorted(set(request.reasoning) - {"effort", "summary"})
            if unsupported_reasoning:
                name = unsupported_reasoning[0]
                raise InvalidRequestError(
                    f"Unsupported reasoning parameter: {name}",
                    param=f"reasoning.{name}",
                    code="unsupported_reasoning_parameter",
                )
        extra = request.model_extra or {}
        supplied_extra = sorted(key for key, value in extra.items() if value is not None)
        if supplied_extra:
            name = supplied_extra[0]
            raise InvalidRequestError(
                f"Unsupported Responses parameter: {name}",
                param=name,
                code="unsupported_parameter",
            )


def _workspace_context(metadata: dict[str, Any] | None) -> _WorkspaceContext:
    public_metadata = dict(metadata or {})
    grant_value = public_metadata.pop("agent_workspace_grant", None)
    checkout_value = public_metadata.pop("agent_checkout_grant", None)
    publish_value = public_metadata.pop("agent_publish_grant", None)
    if grant_value is not None and (
        not isinstance(grant_value, str) or not 16 <= len(grant_value) <= 64 * 1024
    ):
        raise InvalidRequestError(
            "The Workspace grant is invalid.",
            param="metadata.agent_workspace_grant",
            code="workspace_grant_invalid",
        )

    if checkout_value is not None and (
        not isinstance(checkout_value, str) or not 16 <= len(checkout_value) <= 64 * 1024
    ):
        raise InvalidRequestError(
            "The checkout grant is invalid.",
            param="metadata.agent_checkout_grant",
            code="workspace_grant_invalid",
        )
    if publish_value is not None and (
        not isinstance(publish_value, str) or not 16 <= len(publish_value) <= 64 * 1024
    ):
        raise InvalidRequestError(
            "The publish event grant is invalid.",
            param="metadata.agent_publish_grant",
            code="workspace_grant_invalid",
        )
    if checkout_value is not None and grant_value is None:
        raise InvalidRequestError(
            "File checkout requires a Workspace grant.",
            param="metadata.agent_workspace_grant",
            code="workspace_grant_required",
        )
    return _WorkspaceContext(
        grant=grant_value,
        checkout_grant=checkout_value,
        publish_grant=publish_value,
        public_metadata=public_metadata,
    )


def _belongs_to_execution(notification: dict[str, Any], active: ActiveExecution) -> bool:
    params = notification.get("params")
    if not isinstance(params, dict):
        return True
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    if isinstance(thread_id, str) and thread_id != active.thread_id:
        return False
    if isinstance(turn_id, str) and turn_id != active.turn_id:
        return False
    turn = params.get("turn")
    if isinstance(turn, dict):
        nested_turn_id = turn.get("id")
        if isinstance(nested_turn_id, str) and nested_turn_id != active.turn_id:
            return False
    return True


def _item_id(item: dict[str, Any]) -> str | None:
    value = item.get("id")
    return value if isinstance(value, str) else None


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, UpstreamProtocolError):
        logger.warning("Agent execution failure: %s", exc.message)
        logger.debug("Agent execution failure details: %r", exc.details)
        return exc.message
    logger.exception("Unexpected Codex adapter execution failure")
    return "Unexpected Codex adapter execution failure"
