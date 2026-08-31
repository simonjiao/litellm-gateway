from __future__ import annotations

import time
import uuid
from typing import Any

from open_webui.internal.db import AsyncSessionLocal, Base, async_engine
from sqlalchemy import (
    BigInteger,
    Column,
    String,
    Text,
    UniqueConstraint,
    delete,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError


class ChatWorkspace(Base):
    __tablename__ = "agent_chat_workspace"

    chat_id = Column(String, primary_key=True)
    workspace_id = Column(String, nullable=False, unique=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class ConsumedTransferNonce(Base):
    __tablename__ = "agent_consumed_transfer_nonce"

    nonce = Column(String, primary_key=True)
    expires_at = Column(BigInteger, nullable=False)
    consumed_at = Column(BigInteger, nullable=False)


class FileArtifact(Base):
    __tablename__ = "agent_file_artifact"

    file_id = Column(String, primary_key=True)
    artifact_id = Column(String, nullable=False, unique=True)
    owner_user_id = Column(String, nullable=False)
    descriptor_json = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)


class MessageArtifact(Base):
    __tablename__ = "agent_message_artifact"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", "artifact_id", name="agent_message_artifact_uq"),
    )

    id = Column(String, primary_key=True)
    chat_id = Column(String, nullable=False, index=True)
    message_id = Column(String, nullable=False)
    artifact_id = Column(String, nullable=False, index=True)
    direction = Column(String, nullable=False)
    filename = Column(String, nullable=False)
    created_at = Column(BigInteger, nullable=False)


class ResponseBinding(Base):
    __tablename__ = "agent_response_binding"

    assistant_message_id = Column(String, primary_key=True)
    chat_id = Column(String, nullable=False, index=True)
    response_id = Column(String, nullable=False, unique=True)
    workspace_id = Column(String, nullable=False)
    owner_user_id = Column(String, nullable=False)
    created_at = Column(BigInteger, nullable=False)


class PublishIntent(Base):
    __tablename__ = "agent_publish_intent"
    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "assistant_message_id",
            "output_relative_path",
            name="agent_publish_intent_candidate_uq",
        ),
    )

    id = Column(String, primary_key=True)
    chat_id = Column(String, nullable=False, index=True)
    owner_user_id = Column(String, nullable=False)
    workspace_id = Column(String, nullable=False, index=True)
    assistant_message_id = Column(String, nullable=False)
    response_id = Column(String, nullable=False)
    output_relative_path = Column(String, nullable=False)
    state = Column(String, nullable=False, index=True)
    artifact_id = Column(String)
    operation_id = Column(String)
    descriptor_json = Column(Text)
    attempts = Column(BigInteger, nullable=False)
    next_attempt_at = Column(BigInteger, nullable=False, index=True)
    error = Column(Text)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


async def create_tables() -> None:
    async with async_engine.begin() as connection:
        await connection.run_sync(ChatWorkspace.__table__.create, checkfirst=True)
        await connection.run_sync(ConsumedTransferNonce.__table__.create, checkfirst=True)
        await connection.run_sync(FileArtifact.__table__.create, checkfirst=True)
        await connection.run_sync(MessageArtifact.__table__.create, checkfirst=True)
        await connection.run_sync(ResponseBinding.__table__.create, checkfirst=True)
        await connection.run_sync(PublishIntent.__table__.create, checkfirst=True)


async def get_workspace(chat_id: str) -> str | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ChatWorkspace.workspace_id).where(ChatWorkspace.chat_id == chat_id)
        )
        value = result.scalar_one_or_none()
        return str(value) if value is not None else None


async def insert_workspace(chat_id: str, workspace_id: str) -> bool:
    now = int(time.time())
    async with AsyncSessionLocal() as session:
        session.add(
            ChatWorkspace(
                chat_id=chat_id,
                workspace_id=workspace_id,
                created_at=now,
                updated_at=now,
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
    return True


async def delete_workspace(chat_id: str, workspace_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(ChatWorkspace).where(
                ChatWorkspace.chat_id == chat_id,
                ChatWorkspace.workspace_id == workspace_id,
            )
        )
        await session.commit()


async def consume_transfer_nonce(nonce: str, expires_at: int) -> bool:
    now = int(time.time())
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(ConsumedTransferNonce).where(ConsumedTransferNonce.expires_at <= now)
        )
        session.add(
            ConsumedTransferNonce(
                nonce=nonce,
                expires_at=expires_at,
                consumed_at=now,
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
    return True


async def put_file_artifact(
    file_id: str,
    artifact_id: str,
    owner_user_id: str,
    descriptor_json: str,
) -> None:
    now = int(time.time())
    async with AsyncSessionLocal() as session:
        existing = await session.get(FileArtifact, file_id)
        if existing is None:
            session.add(
                FileArtifact(
                    file_id=file_id,
                    artifact_id=artifact_id,
                    owner_user_id=owner_user_id,
                    descriptor_json=descriptor_json,
                    created_at=now,
                )
            )
        elif existing.artifact_id != artifact_id:
            raise RuntimeError("Open WebUI file is already bound to another Artifact")
        await session.commit()


async def get_file_artifact(file_id: str) -> dict[str, Any] | None:
    async with AsyncSessionLocal() as session:
        value = await session.get(FileArtifact, file_id)
        if value is None:
            return None
        return {
            "file_id": value.file_id,
            "artifact_id": value.artifact_id,
            "owner_user_id": value.owner_user_id,
            "descriptor_json": value.descriptor_json,
        }


async def get_file_by_artifact(artifact_id: str) -> dict[str, Any] | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(FileArtifact).where(FileArtifact.artifact_id == artifact_id)
        )
        value = result.scalar_one_or_none()
        if value is None:
            return None
        return {
            "file_id": value.file_id,
            "owner_user_id": value.owner_user_id,
            "descriptor_json": value.descriptor_json,
        }


async def bind_message_artifact(
    chat_id: str,
    message_id: str,
    artifact_id: str,
    direction: str,
    filename: str,
) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            MessageArtifact(
                id=f"binding_{uuid.uuid4().hex}",
                chat_id=chat_id,
                message_id=message_id,
                artifact_id=artifact_id,
                direction=direction,
                filename=filename,
                created_at=int(time.time()),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()


async def message_artifact_chats(artifact_id: str) -> list[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(MessageArtifact.chat_id)
            .where(MessageArtifact.artifact_id == artifact_id)
            .distinct()
        )
        return [str(value) for value in result.scalars().all()]


async def put_response_binding(
    *,
    chat_id: str,
    assistant_message_id: str,
    response_id: str,
    workspace_id: str,
    owner_user_id: str,
) -> None:
    async with AsyncSessionLocal() as session:
        existing = await session.get(ResponseBinding, assistant_message_id)
        if existing is None:
            session.add(
                ResponseBinding(
                    assistant_message_id=assistant_message_id,
                    chat_id=chat_id,
                    response_id=response_id,
                    workspace_id=workspace_id,
                    owner_user_id=owner_user_id,
                    created_at=int(time.time()),
                )
            )
        elif existing.response_id != response_id or existing.chat_id != chat_id:
            raise RuntimeError("Assistant message is already bound to another Response")
        await session.commit()


async def get_response_binding(
    chat_id: str, assistant_message_id: str
) -> dict[str, str] | None:
    async with AsyncSessionLocal() as session:
        binding = await session.get(ResponseBinding, assistant_message_id)
        if binding is None or binding.chat_id != chat_id:
            return None
        return {
            "response_id": str(binding.response_id),
            "workspace_id": str(binding.workspace_id),
            "owner_user_id": str(binding.owner_user_id),
        }


async def create_publish_intent(
    *,
    chat_id: str,
    owner_user_id: str,
    workspace_id: str,
    assistant_message_id: str,
    response_id: str,
    output_relative_path: str,
) -> str:
    now = int(time.time())
    intent_id = f"intent_{uuid.uuid4().hex}"
    async with AsyncSessionLocal() as session:
        session.add(
            PublishIntent(
                id=intent_id,
                chat_id=chat_id,
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                assistant_message_id=assistant_message_id,
                response_id=response_id,
                output_relative_path=output_relative_path,
                state="pending",
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        try:
            await session.commit()
            return intent_id
        except IntegrityError:
            await session.rollback()
            result = await session.execute(
                select(PublishIntent.id).where(
                    PublishIntent.chat_id == chat_id,
                    PublishIntent.assistant_message_id == assistant_message_id,
                    PublishIntent.output_relative_path == output_relative_path,
                )
            )
            existing = result.scalar_one()
            return str(existing)


async def get_publish_intent(intent_id: str) -> dict[str, Any] | None:
    async with AsyncSessionLocal() as session:
        value = await session.get(PublishIntent, intent_id)
        return _intent(value) if value is not None else None


async def get_candidate_intent(
    chat_id: str, assistant_message_id: str, output_relative_path: str
) -> dict[str, Any] | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PublishIntent).where(
                PublishIntent.chat_id == chat_id,
                PublishIntent.assistant_message_id == assistant_message_id,
                PublishIntent.output_relative_path == output_relative_path,
            )
        )
        value = result.scalar_one_or_none()
        return _intent(value) if value is not None else None


async def due_publish_intents(now: int, limit: int = 32) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PublishIntent)
            .where(
                PublishIntent.state.in_(
                    [
                        "pending",
                        "captured",
                        "uploading",
                        "uploaded",
                        "retryable",
                        "binding_retry",
                    ]
                ),
                PublishIntent.next_attempt_at <= now,
            )
            .order_by(PublishIntent.next_attempt_at, PublishIntent.created_at)
            .limit(limit)
        )
        return [_intent(value) for value in result.scalars().all()]


async def capture_barriers(workspace_id: str) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PublishIntent).where(
                PublishIntent.workspace_id == workspace_id,
                PublishIntent.state.in_(["pending"]),
            )
        )
        return [_intent(value) for value in result.scalars().all()]


async def update_publish_intent(intent_id: str, **values: Any) -> None:
    allowed = {
        "state",
        "artifact_id",
        "operation_id",
        "descriptor_json",
        "attempts",
        "next_attempt_at",
        "error",
    }
    if not values.keys() <= allowed:
        raise ValueError("Unsupported publish intent update")
    values["updated_at"] = int(time.time())
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(PublishIntent).where(PublishIntent.id == intent_id).values(**values)
        )
        await session.commit()


def _intent(value: PublishIntent) -> dict[str, Any]:
    return {
        "id": str(value.id),
        "chat_id": str(value.chat_id),
        "owner_user_id": str(value.owner_user_id),
        "workspace_id": str(value.workspace_id),
        "assistant_message_id": str(value.assistant_message_id),
        "response_id": str(value.response_id),
        "output_relative_path": str(value.output_relative_path),
        "state": str(value.state),
        "artifact_id": str(value.artifact_id) if value.artifact_id is not None else None,
        "operation_id": str(value.operation_id) if value.operation_id is not None else None,
        "descriptor_json": (
            str(value.descriptor_json) if value.descriptor_json is not None else None
        ),
        "attempts": int(value.attempts),
        "next_attempt_at": int(value.next_attempt_at),
        "error": str(value.error) if value.error is not None else None,
    }
