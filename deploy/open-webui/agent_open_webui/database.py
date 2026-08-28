from __future__ import annotations

import time

from open_webui.internal.db import AsyncSessionLocal, Base, async_engine
from sqlalchemy import BigInteger, Column, String, delete, select
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


async def create_tables() -> None:
    async with async_engine.begin() as connection:
        await connection.run_sync(ChatWorkspace.__table__.create, checkfirst=True)
        await connection.run_sync(ConsumedTransferNonce.__table__.create, checkfirst=True)


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
