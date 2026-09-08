from __future__ import annotations

import ast
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


def replace_endpoint(route: str, definition: str) -> None:
    path = ROOT / "open_webui/routers/files.py"
    content = path.read_text()
    matches = [
        node
        for node in ast.parse(content).body
        if isinstance(node, ast.AsyncFunctionDef)
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == ("post" if route == "/" else "get")
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
            and decorator.args[0].value == route
            for decorator in node.decorator_list
        )
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Open WebUI v0.11.1 endpoint changed: {route}")
    node = matches[0]
    lines = content.splitlines(keepends=True)
    lines[node.lineno - 1 : node.end_lineno] = [definition + "\n"]
    path.write_text("".join(lines))


# Remove FastAPI's multipart parameter as well as the native storage handler.
# Otherwise Starlette can spool the request to disk before our handler runs.
replace_endpoint(
    "/",
    """async def upload_file(request: Request, user=Depends(get_verified_user)):
    from agent_open_webui.files import upload_file as stream_upload

    result = await stream_upload(request, user)
    await publish_event(
        request, EVENTS.FILE_UPLOADED, actor=user, subject_id=result['id'],
        data={'filename': result['filename'], 'content_type': result['meta']['content_type']},
    )
    return result
""",
)

for route in ("/{id}/content", "/{id}/content/html", "/{id}/content/{file_name}"):
    extra = "file_name: str, " if "{file_name}" in route else ""
    replace_endpoint(
        route,
        f"""async def get_file_content_by_id(
    id: str, {extra}user=Depends(get_verified_user), check: bool = Query(False)
):
    from agent_open_webui.router import uploaded_file_download

    return await uploaded_file_download(id, user, check)
""",
    )

replace_once(
    "open_webui/routers/files.py",
    "                await asyncio.to_thread(Storage.delete_file, file.path)\n"
    "                await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=f'file-{id}')\n",
    "                if file.path:\n"
    "                    await asyncio.to_thread(Storage.delete_file, file.path)\n"
    "                    await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=f'file-{id}')\n",
)

replace_once(
    "open_webui/utils/middleware.py",
    "    __event_emitter__ = extra_params['__event_emitter__']\n    sources = []\n",
    "    # Attachments are checked out to the Agent Workspace by the BFF.\n    return body, {}\n",
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
