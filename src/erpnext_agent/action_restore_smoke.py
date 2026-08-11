from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from redis.asyncio import Redis
from sqlalchemy import delete

from erpnext_agent.actions.gateway import canonicalize_arguments
from erpnext_agent.actions.models import ActionRecord, ActionStatus
from erpnext_agent.auth.session_store import AgentSession, SessionStore
from erpnext_agent.config import get_settings
from erpnext_agent.db import create_engine, create_session_factory


async def run_smoke() -> None:
    """Verify the deployed Action-list API without calling ERPNext or a model."""

    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    session_store = SessionStore(
        redis,
        settings.session_ttl_seconds,
        settings.session_secret.get_secret_value(),
    )
    conversation_id = str(uuid.uuid4())
    owned_action_id = str(uuid.uuid4())
    other_action_id = str(uuid.uuid4())
    owner = "action-restore-smoke@example.invalid"
    agent_session: AgentSession | None = None
    try:
        agent_session = await session_store.create(
            credential_id="action-restore-smoke-credential",
            site=settings.erpnext_site,
            user_id=owner,
        )
        arguments, digest = canonicalize_arguments(
            {
                "doctype": "Material Request",
                "payload": {"company": "Action Restore Smoke"},
            }
        )
        now = datetime.now(UTC)
        records = [
            ActionRecord(
                action_id=owned_action_id,
                session_id=agent_session.session_id,
                site=settings.erpnext_site,
                requested_by=owner,
                tool_name="erpnext_create_draft",
                canonical_arguments=arguments,
                arguments_sha256=digest,
                preview={
                    "title": "Action restore smoke owned",
                    "conversation_id": conversation_id,
                },
                source_versions={},
                idempotency_key=f"action-restore-smoke-owned-{owned_action_id}",
                status=ActionStatus.PENDING.value,
                created_at=now,
                expires_at=now + timedelta(minutes=10),
            ),
            ActionRecord(
                action_id=other_action_id,
                session_id="action-restore-smoke-other-session",
                site=settings.erpnext_site,
                requested_by="other-action-restore-smoke@example.invalid",
                tool_name="erpnext_create_draft",
                canonical_arguments=arguments,
                arguments_sha256=digest,
                preview={
                    "title": "Action restore smoke other user",
                    "conversation_id": conversation_id,
                },
                source_versions={},
                idempotency_key=f"action-restore-smoke-other-{other_action_id}",
                status=ActionStatus.PENDING.value,
                created_at=now,
                expires_at=now + timedelta(minutes=10),
            ),
        ]
        async with session_factory() as db:
            db.add_all(records)
            await db.commit()

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{settings.app_port}",
            timeout=10,
        ) as http:
            response = await http.get(
                f"{settings.api_prefix}/approvals",
                params={"conversation_id": conversation_id},
                cookies={settings.session_cookie_name: agent_session.session_id},
            )
        response.raise_for_status()
        payload = response.json()
        action_ids = [action["action_id"] for action in payload["actions"]]
        if action_ids != [owned_action_id]:
            raise RuntimeError("Action restore API did not preserve user isolation")
        print("Action restore smoke passed: one owned Action, cross-user Action hidden")
    finally:
        async with session_factory() as db:
            await db.execute(
                delete(ActionRecord).where(
                    ActionRecord.action_id.in_([owned_action_id, other_action_id])
                )
            )
            await db.commit()
        if agent_session is not None:
            await session_store.delete(agent_session.session_id)
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    asyncio.run(run_smoke())


if __name__ == "__main__":
    main()
