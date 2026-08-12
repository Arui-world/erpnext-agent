from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from erpnext_agent.config import get_settings
from erpnext_agent.conversations.models import ChatMessageRecord, ConversationRecord
from erpnext_agent.conversations.repository import (
    ConversationNotFoundError,
    ConversationRepository,
)
from erpnext_agent.db import (
    create_engine,
    create_session_factory,
    verify_database_revision,
)


async def run_smoke() -> dict[str, object]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    repository = ConversationRepository()
    now = datetime.now(UTC)
    marker = uuid.uuid4().hex
    user_id = f"conversation-smoke-{marker}@example.invalid"
    ids: set[str] = set()
    message_conversation_ids: set[str] = set()

    try:
        revision = await verify_database_revision(engine)
        async with factory() as session:
            renamed = await repository.resolve_or_create(
                session,
                conversation_id=None,
                site=settings.erpnext_site,
                user_id=user_id,
                mode="model",
            )
            ids.add(renamed.conversation_id)
            message_conversation_ids.add(renamed.conversation_id)
            await repository.append_message(
                session,
                conversation_id=renamed.conversation_id,
                role="user",
                content="retention smoke message",
            )
            await repository.rename_owned(
                session,
                conversation_id=renamed.conversation_id,
                site=settings.erpnext_site,
                user_id=user_id,
                mode="model",
                title="  生命周期\n验收  ",
            )
            if (
                await repository.get_owned(
                    session,
                    conversation_id=renamed.conversation_id,
                    site=settings.erpnext_site,
                    user_id="other@example.invalid",
                    mode="model",
                )
                is not None
            ):
                raise AssertionError("Cross-user conversation read was not isolated")
            try:
                await repository.rename_owned(
                    session,
                    conversation_id=renamed.conversation_id,
                    site=settings.erpnext_site,
                    user_id="other@example.invalid",
                    mode="model",
                    title="unauthorized",
                )
            except ConversationNotFoundError:
                pass
            else:
                raise AssertionError("Cross-user conversation rename was not isolated")

            inactive = await repository.resolve_or_create(
                session,
                conversation_id=None,
                site=settings.erpnext_site,
                user_id=user_id,
                mode="agent",
            )
            ids.add(inactive.conversation_id)
            message_conversation_ids.add(inactive.conversation_id)
            await repository.append_message(
                session,
                conversation_id=inactive.conversation_id,
                role="user",
                content="expired active conversation",
            )
            inactive.created_at = now - timedelta(days=181)
            inactive.updated_at = now - timedelta(days=181)

            empty = await repository.resolve_or_create(
                session,
                conversation_id=None,
                site=settings.erpnext_site,
                user_id=user_id,
                mode="model",
            )
            ids.add(empty.conversation_id)
            empty.created_at = now - timedelta(hours=25)
            empty.updated_at = now - timedelta(hours=25)

            fresh = await repository.resolve_or_create(
                session,
                conversation_id=None,
                site=settings.erpnext_site,
                user_id=user_id,
                mode="model",
            )
            ids.add(fresh.conversation_id)
            await repository.append_message(
                session,
                conversation_id=fresh.conversation_id,
                role="user",
                content="fresh conversation",
            )
            await session.commit()

            summaries = await repository.list_owned(
                session,
                site=settings.erpnext_site,
                user_id=user_id,
                mode="model",
                limit=20,
            )
            renamed_summary = next(
                item
                for item in summaries
                if item.conversation_id == renamed.conversation_id
            )
            if renamed_summary.title != "生命周期 验收":
                raise AssertionError("Persistent conversation title was not returned")

            await repository.soft_delete_owned(
                session,
                conversation_id=renamed.conversation_id,
                site=settings.erpnext_site,
                user_id=user_id,
                mode="model",
            )
            renamed.deleted_at = now - timedelta(days=8)
            await session.commit()
            if (
                await repository.get_owned(
                    session,
                    conversation_id=renamed.conversation_id,
                    site=settings.erpnext_site,
                    user_id=user_id,
                    mode="model",
                )
                is not None
            ):
                raise AssertionError("Soft-deleted conversation remained visible")

        async with factory() as session:
            purged = await repository.purge_expired(
                session,
                now=now,
                retention_days=180,
                deleted_retention_days=7,
                empty_retention_hours=24,
                limit=100,
            )
            await session.commit()
            remaining_ids = set(
                await session.scalars(
                    select(ConversationRecord.conversation_id).where(
                        ConversationRecord.conversation_id.in_(ids)
                    )
                )
            )
            remaining_message_count = int(
                await session.scalar(
                    select(func.count(ChatMessageRecord.message_id)).where(
                        ChatMessageRecord.conversation_id.in_(message_conversation_ids)
                    )
                )
                or 0
            )
            if remaining_ids != {fresh.conversation_id}:
                raise AssertionError(f"Unexpected retained conversations: {remaining_ids}")
            if remaining_message_count != 0:
                raise AssertionError("Purged conversations left cascade-owned messages")

        return {
            "database_revision": revision,
            "renamed": True,
            "cross_user_isolated": True,
            "soft_delete_hidden": True,
            "purged": purged,
            "cascade_messages_remaining": remaining_message_count,
            "fresh_conversation_retained": True,
        }
    finally:
        async with factory() as session:
            await session.execute(
                delete(ConversationRecord).where(
                    ConversationRecord.conversation_id.in_(ids)
                )
            )
            await session.commit()
        await engine.dispose()


def main() -> None:
    print(json.dumps(asyncio.run(run_smoke()), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
