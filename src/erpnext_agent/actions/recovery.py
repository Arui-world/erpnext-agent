from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erpnext_agent.actions.coordination import (
    acquire_action_execution_lease,
    claim_action_recovery_attempt,
)
from erpnext_agent.actions.executor import ActionExecutor
from erpnext_agent.actions.models import ActionRecord, ActionStatus
from erpnext_agent.actions.repository import (
    ActionNotFoundError,
    ActionRepository,
    ActionStateError,
)
from erpnext_agent.auth.session_store import SessionStore
from erpnext_agent.auth.token_refresh import TokenRefreshError, TokenRefreshService
from erpnext_agent.auth.token_store import (
    CredentialDecryptError,
    CredentialNotFoundError,
    TokenStore,
)
from erpnext_agent.coordination import RedisLeaseClient
from erpnext_agent.mcp.adapter import ERPNextMCPAdapter, MCPContractError, MCPError
from erpnext_agent.mcp.refreshing_caller import RefreshingMCPCaller

logger = logging.getLogger(__name__)

RecoveryOutcome = Literal["recovered", "failed", "deferred", "skipped", "error"]


@dataclass(frozen=True, slots=True)
class ActionRecoveryReport:
    scanned: int = 0
    recovered: int = 0
    failed: int = 0
    deferred: int = 0
    skipped: int = 0
    errors: int = 0


class ActionRecoveryWorker:
    """Reconcile uncertain EXECUTING Actions with their original idempotency key."""

    def __init__(
        self,
        *,
        enabled: bool,
        session_factory: async_sessionmaker[AsyncSession],
        redis: RedisLeaseClient,
        repository: ActionRepository,
        token_store: TokenStore,
        refresh_service: TokenRefreshService,
        session_store: SessionStore,
        adapter: ERPNextMCPAdapter,
        poll_seconds: int,
        retry_seconds: int,
        batch_size: int,
        execution_lock_ttl_seconds: int,
    ) -> None:
        self.enabled = enabled
        self._session_factory = session_factory
        self._redis = redis
        self._repository = repository
        self._token_store = token_store
        self._refresh_service = refresh_service
        self._session_store = session_store
        self._adapter = adapter
        self._poll_seconds = poll_seconds
        self._retry_seconds = retry_seconds
        self._batch_size = batch_size
        self._execution_lock_ttl_seconds = execution_lock_ttl_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_report: ActionRecoveryReport | None = None
        self.last_completed_at: datetime | None = None

    @property
    def running(self) -> bool:
        return bool(self._task is not None and not self._task.done())

    def start(self) -> None:
        if not self.enabled or self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_forever(),
            name="action-recovery-worker",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        await self._task
        self._task = None

    async def run_once(self) -> ActionRecoveryReport:
        async with self._session_factory() as session:
            action_ids = await self._repository.list_executing_ids(
                session,
                limit=self._batch_size,
            )

        counts: dict[RecoveryOutcome, int] = {
            "recovered": 0,
            "failed": 0,
            "deferred": 0,
            "skipped": 0,
            "error": 0,
        }
        for action_id in action_ids:
            try:
                claimed = await claim_action_recovery_attempt(
                    self._redis,
                    action_id=action_id,
                    cooldown_seconds=self._retry_seconds,
                )
                if not claimed:
                    counts["deferred"] += 1
                    continue
                outcome = await self._recover_one(action_id)
            except Exception:
                logger.exception("Action recovery attempt failed", extra={"action_id": action_id})
                outcome = "error"
            counts[outcome] += 1

        report = ActionRecoveryReport(
            scanned=len(action_ids),
            recovered=counts["recovered"],
            failed=counts["failed"],
            deferred=counts["deferred"],
            skipped=counts["skipped"],
            errors=counts["error"],
        )
        self.last_report = report
        self.last_completed_at = datetime.now(UTC)
        return report

    async def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("Action recovery scan failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_seconds,
                )
            except TimeoutError:
                continue

    async def _recover_one(self, action_id: str) -> RecoveryOutcome:
        lease = await acquire_action_execution_lease(
            self._redis,
            action_id=action_id,
            ttl_seconds=self._execution_lock_ttl_seconds,
        )
        if lease is None:
            return "deferred"

        async with lease:
            async with self._session_factory() as session:
                try:
                    action = await self._repository.get_executing(
                        session,
                        action_id=action_id,
                    )
                except ActionNotFoundError:
                    return "skipped"

                if action.decided_by != action.requested_by:
                    await self._record_deferred_failure(
                        session,
                        action,
                        code="ACTION_DECISION_IDENTITY_MISMATCH",
                        message="Approved Action owner and decision identity do not match",
                    )
                    return "error"

                try:
                    credential = await self._token_store.get_for_user(
                        session,
                        site=action.site,
                        user_id=action.requested_by,
                    )
                    if not self._credential_matches_action(
                        action,
                        credential.site,
                        credential.user_id,
                    ):
                        raise TokenRefreshError("OAuth credential identity does not match Action")
                    credential = await self._refresh_service.get_valid(
                        session,
                        credential.credential_id,
                    )
                except (
                    CredentialDecryptError,
                    CredentialNotFoundError,
                    TokenRefreshError,
                ) as exc:
                    await self._session_store.delete(action.session_id)
                    await self._record_deferred_failure(
                        session,
                        action,
                        code="REAUTHENTICATION_REQUIRED",
                        message=str(exc),
                    )
                    return "deferred"

                caller = RefreshingMCPCaller(
                    adapter=self._adapter,
                    refresh_service=self._refresh_service,
                    db=session,
                    credential=credential,
                    session_store=self._session_store,
                    agent_session_id=action.session_id,
                )
                identity_outcome = await self._verify_identity(
                    session,
                    action=action,
                    caller=caller,
                    access_token=credential.access_token,
                )
                if identity_outcome is not None:
                    return identity_outcome

                try:
                    await ActionExecutor(self._repository, caller).reconcile(
                        session,
                        action=action,
                        access_token=credential.access_token,
                    )
                except MCPError:
                    return (
                        "failed"
                        if action.status == ActionStatus.FAILED.value
                        else "deferred"
                    )
                except ActionStateError as exc:
                    await self._record_deferred_failure(
                        session,
                        action,
                        code="ACTION_STATE_INVALID",
                        message=str(exc),
                    )
                    return "error"
                return "recovered"

    async def _verify_identity(
        self,
        session: AsyncSession,
        *,
        action: ActionRecord,
        caller: RefreshingMCPCaller,
        access_token: str,
    ) -> RecoveryOutcome | None:
        try:
            identity = await caller.call_tool(
                access_token=access_token,
                name="erpnext_get_current_user",
                arguments={},
            )
            user = identity.data.get("user") if isinstance(identity.data, dict) else None
            if not isinstance(user, str):
                raise MCPContractError(
                    "Current-user response has no user",
                    code="INVALID_IDENTITY",
                )
        except MCPError as exc:
            await self._record_deferred_failure(
                session,
                action,
                code=exc.code,
                message=str(exc),
            )
            return "deferred"

        if not secrets.compare_digest(user.casefold(), action.requested_by.casefold()):
            await self._session_store.delete(action.session_id)
            await self._record_deferred_failure(
                session,
                action,
                code="IDENTITY_MISMATCH",
                message="OAuth and MCP identities do not match the Action owner",
            )
            return "error"
        return None

    @staticmethod
    def _credential_matches_action(
        action: ActionRecord,
        credential_site: str,
        credential_user: str,
    ) -> bool:
        return secrets.compare_digest(action.site, credential_site) and secrets.compare_digest(
            action.requested_by.casefold(),
            credential_user.casefold(),
        )

    @staticmethod
    async def _record_deferred_failure(
        session: AsyncSession,
        action: ActionRecord,
        *,
        code: str,
        message: str,
    ) -> None:
        action.failure_code = code
        action.failure_message = message
        await session.commit()
