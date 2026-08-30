from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/app/backend")


def replace_once(relative: str, old: str, new: str) -> None:
    path = ROOT / relative
    content = path.read_text()
    if content.count(old) != 1:
        raise RuntimeError(f"Open WebUI v0.11.1 patch anchor changed: {relative}")
    path.write_text(content.replace(old, new, 1))


replace_once(
    "open_webui/routers/openai.py",
    "    requested_model = payload.get('model')\n",
    "    if is_responses:\n"
    "        from agent_open_webui.workspace import inject_workspace_context\n"
    "\n"
    "        payload = await inject_workspace_context(request, user, metadata, payload)\n"
    "    requested_model = payload.get('model')\n",
)

replace_once(
    "open_webui/routers/files.py",
    "    result = await upload_file_handler(\n"
    "        request,\n"
    "        file=file,\n"
    "        metadata=metadata,\n"
    "        process=process,\n"
    "        process_in_background=process_in_background,\n"
    "        user=user,\n"
    "        background_tasks=background_tasks,\n"
    "        db=db,\n"
    "    )\n\n"
    "    if isinstance(result, dict):\n",
    "    result = await upload_file_handler(\n"
    "        request,\n"
    "        file=file,\n"
    "        metadata=metadata,\n"
    "        process=process,\n"
    "        process_in_background=process_in_background,\n"
    "        user=user,\n"
    "        background_tasks=background_tasks,\n"
    "        db=db,\n"
    "    )\n"
    "    from agent_open_webui.workspace import register_uploaded_file\n\n"
    "    await register_uploaded_file(request, user, result)\n\n"
    "    if isinstance(result, dict):\n",
)

replace_once(
    "open_webui/routers/files.py",
    "async def get_file_content_by_id(\n"
    "    id: str,\n"
    "    user=Depends(get_verified_user),\n"
    "    attachment: bool = Query(False),\n"
    "    db: AsyncSession = Depends(get_async_session),\n"
    "):\n"
    "    file = await Files.get_file_by_id(id, db=db)\n",
    "async def get_file_content_by_id(\n"
    "    id: str,\n"
    "    user=Depends(get_verified_user),\n"
    "    attachment: bool = Query(False),\n"
    "    db: AsyncSession = Depends(get_async_session),\n"
    "):\n"
    "    from agent_open_webui.router import uploaded_file_download\n\n"
    "    artifact_response = await uploaded_file_download(id, user)\n"
    "    if artifact_response is not None:\n"
    "        return artifact_response\n"
    "    file = await Files.get_file_by_id(id, db=db)\n",
)

replace_once(
    "open_webui/routers/chats.py",
    "        await Chats.delete_chat_by_id_and_user_id(child_id, chat.user_id)\n",
    "        await Chats.delete_chat_by_id_and_user_id(child_id, chat.user_id)\n"
    "        from agent_open_webui.workspace import release_chat_workspace\n"
    "\n"
    "        await release_chat_workspace(child_id)\n",
)

replace_once(
    "open_webui/routers/chats.py",
    "    if result:\n"
    "        await publish_event(\n"
    "            request,\n"
    "            EVENTS.CHAT_DELETED,\n",
    "    if result:\n"
    "        from agent_open_webui.workspace import release_chat_workspace\n"
    "\n"
    "        await release_chat_workspace(id)\n"
    "        await publish_event(\n"
    "            request,\n"
    "            EVENTS.CHAT_DELETED,\n",
)

replace_once(
    "open_webui/main.py",
    "    await initialize_runtime_config(app)\n    await migrate_legacy_webhook_config()\n",
    "    await initialize_runtime_config(app)\n"
    "    from agent_open_webui.workspace import startup_workspace_bridge\n"
    "\n"
    "    await startup_workspace_bridge()\n"
    "    await migrate_legacy_webhook_config()\n",
)

replace_once(
    "open_webui/main.py",
    "    from open_webui.utils.session_pool import close_session\n\n    await close_session()\n",
    "    from open_webui.utils.session_pool import close_session\n"
    "    from agent_open_webui.workspace import shutdown_workspace_bridge\n"
    "\n"
    "    await shutdown_workspace_bridge()\n"
    "    await close_session()\n",
)

replace_once(
    "open_webui/main.py",
    "app.include_router(automations.router, prefix='/api/v1/automations', tags=['automations'])\n",
    "app.include_router(automations.router, prefix='/api/v1/automations', tags=['automations'])\n"
    "from agent_open_webui.router import router as agent_workspace_router\n"
    "\n"
    "app.include_router(agent_workspace_router, prefix='/api/agent', tags=['agent-workspace'])\n",
)
