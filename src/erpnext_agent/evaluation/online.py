"""Authenticated online evaluation executor.

Drives the *running* Agent deployment with real per-user OAuth identities already
stored in PostgreSQL. Unlike the offline deterministic runner, this executor measures
live model behavior, ERPNext data handling, and per-user isolation. It is intentionally
decoupled from the offline path: ``runner.main`` imports this module lazily only when a
suite declares ``execution_mode == "online_authenticated"``.

Heavy dependencies (httpx, Redis, SQLAlchemy) and every external side effect are reached
through small injectable seams so the executor is fully unit-testable offline:

- ``credential_lookup``: user_id -> credential_id | None
- ``session_provider``: (credential_id, user_id) -> AgentSession
- ``token_provider``: credential_id -> decrypted access token
- ``draft_deleter``: (doctype, name, token) -> HTTP status code | None
- ``transport``: the :class:`ChatTransport` used for all chat/approval HTTP calls

Production wires these to PostgreSQL, Redis, TokenStore, ERPNext REST and the live HTTP
API respectively. Drafts created by approved end-to-end cases are deleted afterwards via
``draft_deleter`` (a test-cleanup path only).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

import httpx
from pydantic import JsonValue
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from erpnext_agent.auth.models import OAuthCredentialRecord
from erpnext_agent.auth.session_store import AgentSession, SessionStore
from erpnext_agent.auth.token_store import (
    CredentialDecryptError,
    CredentialNotFoundError,
    TokenStore,
)
from erpnext_agent.config import Settings, get_settings
from erpnext_agent.db import create_engine, create_session_factory
from erpnext_agent.evaluation.online_assertions import (
    assert_no_tools,
    assert_text_substrings,
    assert_tool_sequence,
    looks_permission_denied,
)
from erpnext_agent.evaluation.online_cleanup import CleanupTarget, DraftCleanupRegistry
from erpnext_agent.evaluation.online_transport import (
    ChatTransport,
    HttpChatTransport,
    LiveStreamReply,
    parse_chat_stream,
)
from erpnext_agent.evaluation.schema import (
    CategoryResult,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationCategory,
    EvaluationReport,
    EvaluationSuite,
    EvaluationSummary,
    LiveChatCase,
    LiveDraftActionCase,
)

ONLINE_DISCLAIMER = (
    "Authenticated online evaluation against a live deployment. These results measure "
    "real model behavior, ERPNext data handling, OAuth permission boundaries and "
    "end-to-end draft flows. They depend on live fixture data and are not comparable to "
    "the offline deterministic policy baseline."
)

WRITE_TOOLS: tuple[str, ...] = ("erpnext_create_draft", "erpnext_update_draft")
NORMAL_FINISHED_REASONS = frozenset({"completed", "stop", "done"})

FIXTURE_ENV: dict[str, str] = {
    "company": "EVAL_ONLINE_COMPANY",
    "customer": "EVAL_ONLINE_CUSTOMER",
    "supplier": "EVAL_ONLINE_SUPPLIER",
    "item": "EVAL_ONLINE_ITEM",
    "warehouse": "EVAL_ONLINE_WAREHOUSE",
}
DATE_KEYS: tuple[str, ...] = ("today", "delivery_date", "schedule_date")
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

PRIMARY_USER_ENV = "EVAL_ONLINE_PRIMARY_USER_ID"
SECONDARY_USER_ENV = "EVAL_ONLINE_SECONDARY_USER_ID"
AGENT_URL_ENV = "EVAL_ONLINE_AGENT_URL"
DEFAULT_AGENT_URL = "http://agent:8001"

CredentialLookup = Callable[[str], Awaitable[str | None]]
SessionProvider = Callable[[str, str], Awaitable[AgentSession]]
TokenProvider = Callable[[str], Awaitable[str]]
DraftDeleter = Callable[[str, str, str], Awaitable[int | None]]


class OnlineEnvironmentError(RuntimeError):
    """Environment prerequisites for online evaluation are missing."""


@dataclass(frozen=True, slots=True)
class OnlineEnvironment:
    primary_user_id: str
    secondary_user_id: str | None
    agent_url: str
    fixtures: dict[str, str]
    dates: dict[str, str]
    environ: Mapping[str, str]

    def user_id_for(self, identity: str) -> str | None:
        if identity == "primary":
            return self.primary_user_id
        return self.secondary_user_id

    def render(self, text: str) -> str:
        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in self.fixtures:
                return self.fixtures[key]
            if key in self.dates:
                return self.dates[key]
            return match.group(0)

        return PLACEHOLDER_RE.sub(substitute, text)


@dataclass(slots=True)
class ResolvedIdentity:
    identity: str
    user_id: str
    credential_id: str | None
    session: AgentSession | None

    @property
    def available(self) -> bool:
        return self.credential_id is not None and self.session is not None


class OnlineEvaluationRunner:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        environ: Mapping[str, str] | None = None,
        transport: ChatTransport | None = None,
        credential_lookup: CredentialLookup | None = None,
        session_provider: SessionProvider | None = None,
        token_provider: TokenProvider | None = None,
        draft_deleter: DraftDeleter | None = None,
        case_timeout_seconds: float = 600.0,
    ) -> None:
        self._settings_input = settings
        self._settings: Settings | None = settings
        import os

        self._environ: Mapping[str, str] = (
            environ if environ is not None else cast(Mapping[str, str], os.environ)
        )
        self._transport_input = transport
        self._credential_lookup_input = credential_lookup
        self._session_provider_input = session_provider
        self._token_provider_input = token_provider
        self._draft_deleter_input = draft_deleter
        self._case_timeout_seconds = case_timeout_seconds

        self._transport: ChatTransport | None = transport
        self._credential_lookup: CredentialLookup | None = credential_lookup
        self._session_provider: SessionProvider | None = session_provider
        self._token_provider: TokenProvider | None = token_provider
        self._draft_deleter: DraftDeleter | None = draft_deleter
        self._cleanup = DraftCleanupRegistry()

        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._redis: Redis | None = None
        self._session_store: SessionStore | None = None
        self._token_store: TokenStore | None = None
        self._http: httpx.AsyncClient | None = None

    # -- public API ---------------------------------------------------------

    async def run(self, suite: EvaluationSuite) -> EvaluationReport:
        env = self.resolve_environment(suite)
        await self._prepare(env)
        results: list[EvaluationCaseResult] = []
        try:
            identities = await self._resolve_identities(env, suite)
            for case in suite.cases:
                results.append(await self._execute_case(case, env, identities))
        finally:
            leftovers = await self._drain_cleanup()
            for item in leftovers:
                if not item.get("deleted"):
                    # Leftover drafts are an operational concern, not a scored case;
                    # surface them loudly for manual follow-up.
                    print(
                        f"WARNING: leftover evaluation draft not cleaned: "
                        f"{item.get('doctype')} {item.get('name')} "
                        f"(status={item.get('status_code')}, error={item.get('error')})",
                        flush=True,
                    )
            await self._close()
        return self._build_report(suite, results)

    # -- environment resolution --------------------------------------------

    def resolve_environment(self, suite: EvaluationSuite) -> OnlineEnvironment:
        env = self._environ
        missing: list[str] = []

        primary = env.get(PRIMARY_USER_ENV, "").strip()
        if not primary:
            missing.append(PRIMARY_USER_ENV)

        needs_secondary = _needs_secondary(suite)
        secondary_raw = env.get(SECONDARY_USER_ENV, "").strip()
        if needs_secondary and not secondary_raw:
            missing.append(SECONDARY_USER_ENV)

        agent_url = env.get(AGENT_URL_ENV, "").strip() or DEFAULT_AGENT_URL

        texts = _collect_placeholder_texts(suite)
        referenced = set()
        for text in texts:
            referenced.update(PLACEHOLDER_RE.findall(text))

        fixtures: dict[str, str] = {}
        for key in sorted(referenced):
            if key in DATE_KEYS:
                continue
            if key not in FIXTURE_ENV:
                raise OnlineEnvironmentError(
                    f"Evaluation case references unknown placeholder '{{{key}}}'; "
                    f"known placeholders are {sorted(FIXTURE_ENV) + list(DATE_KEYS)}."
                )
            value = env.get(FIXTURE_ENV[key], "").strip()
            if not value:
                missing.append(FIXTURE_ENV[key])
            fixtures[key] = value

        if missing:
            raise OnlineEnvironmentError(
                "Online evaluation environment is incomplete. Missing values: "
                f"{sorted(set(missing))}. Provide them as environment variables before "
                "running the online suite, and make sure both users have logged in via "
                "browser OAuth so their credentials exist."
            )

        return OnlineEnvironment(
            primary_user_id=primary,
            secondary_user_id=secondary_raw or None,
            agent_url=agent_url,
            fixtures=fixtures,
            dates=_date_placeholders(),
            environ=env,
        )

    # -- resource lifecycle -------------------------------------------------

    def _require_settings(self) -> Settings:
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    async def _prepare(self, env: OnlineEnvironment) -> None:
        # Settings are resolved lazily so a fully-injected runner (unit tests) never
        # reads the environment or constructs production resources.
        needs_db = (
            self._credential_lookup_input is None or self._token_provider_input is None
        )
        needs_redis = self._session_provider_input is None
        needs_http = self._transport_input is None or self._draft_deleter_input is None

        if needs_db:
            settings = self._require_settings()
            self._engine = create_engine(settings.database_url)
            self._session_factory = create_session_factory(self._engine)
        if needs_redis:
            settings = self._require_settings()
            self._redis = Redis.from_url(
                settings.redis_url.get_secret_value(), decode_responses=True
            )
            self._session_store = SessionStore(
                self._redis,
                settings.session_ttl_seconds,
                settings.session_secret.get_secret_value(),
            )
        if needs_http:
            self._http = httpx.AsyncClient()

        if self._transport_input is None:
            settings = self._require_settings()
            assert self._http is not None
            self._transport = HttpChatTransport(
                client=self._http,
                base_url=env.agent_url,
                api_prefix=settings.api_prefix,
                cookie_name=settings.session_cookie_name,
                timeout_seconds=self._case_timeout_seconds,
            )
        if self._credential_lookup_input is None:
            self._credential_lookup = self._default_credential_lookup
        if self._session_provider_input is None:
            self._session_provider = self._default_session_provider
        if self._token_provider_input is None:
            self._token_provider = self._default_token_provider
        if self._draft_deleter_input is None:
            self._draft_deleter = self._default_draft_deleter

    async def _close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
        if self._redis is not None:
            await self._redis.aclose()
        if self._engine is not None:
            await self._engine.dispose()

    # -- default seam implementations ---------------------------------------

    async def _default_credential_lookup(self, user_id: str) -> str | None:
        assert self._session_factory is not None
        settings = self._require_settings()
        async with self._session_factory() as db:
            credential_id = await db.scalar(
                select(OAuthCredentialRecord.credential_id)
                .where(
                    OAuthCredentialRecord.site == settings.erpnext_site,
                    OAuthCredentialRecord.user_id == user_id,
                    OAuthCredentialRecord.revoked_at.is_(None),
                )
                .order_by(OAuthCredentialRecord.updated_at.desc())
                .limit(1),
            )
        return str(credential_id) if credential_id is not None else None

    async def _default_session_provider(
        self, credential_id: str, user_id: str
    ) -> AgentSession:
        assert self._session_store is not None
        assert self._session_factory is not None
        settings = self._require_settings()
        async with self._session_factory() as db:
            binding_id = await db.scalar(
                select(OAuthCredentialRecord.binding_id).where(
                    OAuthCredentialRecord.credential_id == credential_id,
                    OAuthCredentialRecord.site == settings.erpnext_site,
                    OAuthCredentialRecord.user_id == user_id,
                    OAuthCredentialRecord.revoked_at.is_(None),
                )
            )
        if binding_id is None:
            raise CredentialNotFoundError(credential_id)
        return await self._session_store.create(
            credential_id=credential_id,
            binding_id=str(binding_id),
            site=settings.erpnext_site,
            user_id=user_id,
        )

    async def _default_token_provider(self, credential_id: str) -> str:
        if self._token_store is None:
            settings = self._require_settings()
            self._token_store = TokenStore(
                encryption_key=settings.token_encryption_key.get_secret_value(),
                key_version=settings.token_encryption_key_version,
                client_id=settings.oauth_client_id,
            )
        assert self._session_factory is not None
        async with self._session_factory() as db:
            credential = await self._token_store.get(db, credential_id)
        return credential.access_token

    async def _default_draft_deleter(
        self, doctype: str, name: str, access_token: str
    ) -> int | None:
        assert self._http is not None
        settings = self._require_settings()
        url = (
            f"{settings.effective_erpnext_internal_url}/api/resource/"
            f"{quote(doctype, safe='')}/{quote(name, safe='')}"
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Host": settings.erpnext_host_header,
            "Accept": "application/json",
        }
        try:
            response = await self._http.delete(url, headers=headers, timeout=30.0)
        except httpx.HTTPError:
            return None
        return response.status_code

    # -- identity resolution ------------------------------------------------

    async def _resolve_identities(
        self, env: OnlineEnvironment, suite: EvaluationSuite
    ) -> dict[str, ResolvedIdentity]:
        needed = _needed_identities(suite)
        resolved: dict[str, ResolvedIdentity] = {}
        assert self._credential_lookup is not None
        assert self._session_provider is not None
        for identity in sorted(needed):
            user_id = env.user_id_for(identity)
            if user_id is None:
                resolved[identity] = ResolvedIdentity(identity, "", None, None)
                continue
            credential_id = await self._credential_lookup(user_id)
            session = None
            if credential_id is not None:
                session = await self._session_provider(credential_id, user_id)
            resolved[identity] = ResolvedIdentity(identity, user_id, credential_id, session)

        if resolved and not any(item.available for item in resolved.values()):
            users = sorted(
                {item.user_id for item in resolved.values() if item.user_id}
            )
            raise OnlineEnvironmentError(
                "No required evaluation identity has an active OAuth credential. Log in "
                f"as {users} through the browser OAuth flow first, then re-run the suite."
            )
        return resolved

    # -- case execution -----------------------------------------------------

    async def _execute_case(
        self,
        case: EvaluationCase,
        env: OnlineEnvironment,
        identities: dict[str, ResolvedIdentity],
    ) -> EvaluationCaseResult:
        started = time.perf_counter()
        try:
            if isinstance(case, LiveChatCase):
                passed, evidence = await asyncio.wait_for(
                    self._execute_live_chat(case, env, identities),
                    timeout=self._case_timeout_seconds,
                )
            elif isinstance(case, LiveDraftActionCase):
                passed, evidence = await asyncio.wait_for(
                    self._execute_live_draft_action(case, env, identities),
                    timeout=self._case_timeout_seconds,
                )
            else:
                passed = False
                evidence = {"error_type": "UNSUPPORTED_ONLINE_EXECUTOR"}
        except OnlineIdentityMissing as exc:
            passed = False
            evidence = {"error_type": "IDENTITY_MISSING", "identity": exc.identity}
        except TimeoutError:
            passed = False
            evidence = {"error_type": "CASE_TIMEOUT"}
        except Exception as exc:  # an evaluator bug must fail closed, not abort the suite
            passed = False
            evidence = {"error_type": type(exc).__name__}
        duration_ms = (time.perf_counter() - started) * 1000
        return EvaluationCaseResult(
            case_id=case.case_id,
            category=case.category,
            executor=case.executor,
            security_critical=case.security_critical,
            passed=passed,
            duration_ms=round(duration_ms, 3),
            evidence=evidence,
        )

    def _session_for(
        self, identities: dict[str, ResolvedIdentity], identity: str
    ) -> tuple[AgentSession, str]:
        resolved = identities.get(identity)
        if resolved is None or not resolved.available:
            raise OnlineIdentityMissing(identity)
        assert resolved.session is not None
        assert resolved.credential_id is not None
        return resolved.session, resolved.credential_id

    async def _execute_live_chat(
        self,
        case: LiveChatCase,
        env: OnlineEnvironment,
        identities: dict[str, ResolvedIdentity],
    ) -> tuple[bool, dict[str, Any]]:
        session, _ = self._session_for(identities, case.input.identity)
        assert self._transport is not None

        conversation_id: str | None = None
        replies: list[LiveStreamReply] = []
        turn_tool_details: list[dict[str, Any]] = []
        for turn in case.input.turns:
            message = env.render(turn.message)
            reply = await parse_chat_stream(
                self._transport.stream_chat(
                    session=session, message=message, conversation_id=conversation_id
                )
            )
            if reply.conversation_id is not None:
                conversation_id = reply.conversation_id
            replies.append(reply)
            if turn.expected_route is not None and reply.route != turn.expected_route:
                turn_tool_details.append(
                    {
                        "expected_route": turn.expected_route,
                        "actual_route": reply.route,
                        "matched": False,
                    }
                )
            elif turn.expected_route is not None:
                turn_tool_details.append(
                    {
                        "expected_route": turn.expected_route,
                        "actual_route": reply.route,
                        "matched": True,
                    }
                )
            if turn.expected_tools:
                _, details = assert_tool_sequence(reply.tool_calls, turn.expected_tools)
                turn_tool_details.append(details)

        all_tools = [tool for reply in replies for tool in reply.tool_calls]
        full_text = "\n".join(reply.text for reply in replies if reply.text)
        error_events = [reply.error for reply in replies if reply.error is not None]

        expected = case.expected
        forbidden = list(set(expected.forbidden_tools) | set(WRITE_TOOLS))

        tool_ok, tool_details = assert_tool_sequence(all_tools, expected.expected_tools)
        forbidden_ok, forbidden_details = assert_no_tools(all_tools, forbidden)
        text_ok, text_details = assert_text_substrings(
            full_text,
            must_contain=[env.render(s) for s in expected.text_must_contain],
            must_contain_any=[env.render(s) for s in expected.text_must_contain_any],
            must_not_contain=[env.render(s) for s in expected.text_must_not_contain],
        )
        facts_ok, fact_details = self._check_env_facts(full_text, expected.optional_env_facts)
        error_ok = (len(error_events) > 0) if expected.expect_error else not error_events
        completion_ok = (
            not expected.require_normal_completion
            or expected.expect_error
            or all(reply.finished_reason in NORMAL_FINISHED_REASONS for reply in replies)
        )

        continuity_ok = True
        if len(case.input.turns) > 1:
            continuity_ok = conversation_id is not None

        route_ok = all(
            detail.get("matched", True)
            for detail in turn_tool_details
            if "expected_route" in detail
        )

        passed = (
            tool_ok
            and forbidden_ok
            and text_ok
            and facts_ok
            and error_ok
            and continuity_ok
            and route_ok
            and completion_ok
        )
        if expected.allow_permission_denied and not passed:
            passed = completion_ok and not error_events and looks_permission_denied(full_text)

        evidence: dict[str, Any] = {
            "identity": case.input.identity,
            "conversation_id": conversation_id,
            "turns": len(case.input.turns),
            "tool_calls": all_tools,
            "routes": [reply.route for reply in replies],
            "tool_sequence": tool_details,
            "forbidden_tools": forbidden_details,
            "text": text_details,
            "env_facts": fact_details,
            "error_events": error_events,
            "finished_reasons": [reply.finished_reason for reply in replies],
            "normal_completion": completion_ok,
            "reply_excerpt": full_text[:500],
        }
        if turn_tool_details:
            evidence["turn_tool_sequences"] = turn_tool_details
        return passed, evidence

    def _check_env_facts(
        self, text: str, env_fact_names: list[str]
    ) -> tuple[bool, dict[str, Any]]:
        checked: dict[str, JsonValue] = {}
        passed = True
        for name in env_fact_names:
            value = self._environ.get(name, "").strip()
            if not value:
                checked[name] = "skipped_not_configured"
                continue
            present = value in text
            checked[name] = present
            passed = passed and present
        return passed, {"facts": checked}

    async def _execute_live_draft_action(
        self,
        case: LiveDraftActionCase,
        env: OnlineEnvironment,
        identities: dict[str, ResolvedIdentity],
    ) -> tuple[bool, dict[str, Any]]:
        session, credential_id = self._session_for(identities, case.input.identity)
        assert self._transport is not None
        expected = case.expected

        message = env.render(case.input.message)
        reply = await parse_chat_stream(
            self._transport.stream_chat(
                session=session, message=message, conversation_id=None
            )
        )
        evidence: dict[str, Any] = {
            "identity": case.input.identity,
            "doctype": case.input.doctype,
            "decision": expected.decision,
            "tool_calls": list(reply.tool_calls),
            "proposed": reply.action is not None,
            "finished_reason": reply.finished_reason,
        }
        if reply.error is not None:
            evidence["error_events"] = [reply.error]
            return False, evidence
        if (
            expected.require_normal_completion
            and reply.finished_reason not in NORMAL_FINISHED_REASONS
        ):
            evidence["reason"] = "agent reply did not finish normally"
            evidence["reply_excerpt"] = reply.text[:500]
            return False, evidence
        if reply.action is None:
            evidence["reason"] = "no action_required event emitted"
            evidence["reply_excerpt"] = reply.text[:500]
            return False, evidence

        action = reply.action
        action_id = str(action.get("action_id", ""))
        evidence["action_id"] = action_id
        preview = action.get("preview")
        if isinstance(preview, dict):
            evidence["preview_doctype"] = preview.get("doctype")
        if not action_id:
            evidence["reason"] = "action_required event missing action_id"
            return False, evidence

        if expected.cross_user_identity is not None:
            attacker_session, _ = self._session_for(
                identities, expected.cross_user_identity
            )
            decision_status, _ = await self._transport.post_json(
                session=attacker_session,
                path=f"/approvals/{action_id}/decision",
                payload={"decision": "approve"},
            )
            execute_status, _ = await self._transport.post_json(
                session=attacker_session,
                path=f"/approvals/{action_id}/execute",
                payload=None,
            )
            evidence["cross_user_identity"] = expected.cross_user_identity
            evidence["cross_user_decision_status"] = decision_status
            evidence["cross_user_execute_status"] = execute_status
            if decision_status != 404 or execute_status != 404:
                evidence["reason"] = "cross-user access was not denied with 404"
                return False, evidence

        decision_status, decision_body = await self._transport.post_json(
            session=session,
            path=f"/approvals/{action_id}/decision",
            payload={"decision": expected.decision},
        )
        evidence["decision_status"] = decision_status
        evidence["decision_result_status"] = decision_body.get("status")

        if expected.decision == "reject":
            passed = decision_status == 200 and decision_body.get("status") == "REJECTED"
            evidence["executed"] = False
            return passed, evidence

        if decision_status != 200 or decision_body.get("status") != "APPROVED":
            evidence["reason"] = "approval did not reach APPROVED"
            return False, evidence

        execute_status, execute_body = await self._transport.post_json(
            session=session,
            path=f"/approvals/{action_id}/execute",
            payload=None,
        )
        evidence["execute_status"] = execute_status
        evidence["execute_result_status"] = execute_body.get("status")
        if execute_status != 200:
            evidence["execute_detail"] = execute_body
        result_reference = execute_body.get("result_reference")
        evidence["result_reference"] = (
            result_reference if isinstance(result_reference, dict) else None
        )

        executed_ok = execute_status == 200 and execute_body.get("status") == "SUCCEEDED"
        docstatus_ok = (
            isinstance(result_reference, dict) and result_reference.get("docstatus") == 0
        )
        passed = executed_ok and docstatus_ok and expected.expect_execute_success

        if executed_ok and isinstance(result_reference, dict) and expected.expect_cleanup:
            doctype = str(result_reference.get("doctype") or case.input.doctype)
            name = str(result_reference.get("name") or "")
            if name:
                self._cleanup.register(
                    doctype=doctype, name=name, credential_id=credential_id
                )
                cleanup_outcome = await self._delete_target(
                    CleanupTarget(doctype=doctype, name=name, credential_id=credential_id)
                )
                evidence["cleanup"] = cleanup_outcome
                if cleanup_outcome.get("deleted"):
                    self._cleanup.forget(doctype=doctype, name=name)
        return passed, evidence

    # -- cleanup ------------------------------------------------------------

    async def _delete_target(self, target: CleanupTarget) -> dict[str, JsonValue]:
        outcome: dict[str, JsonValue] = {
            "doctype": target.doctype,
            "name": target.name,
            "deleted": False,
            "status_code": None,
            "error": None,
        }
        assert self._token_provider is not None
        assert self._draft_deleter is not None
        try:
            token = await self._token_provider(target.credential_id)
        except (CredentialNotFoundError, CredentialDecryptError) as exc:
            outcome["error"] = f"credential unavailable: {type(exc).__name__}"
            return outcome
        try:
            status = await self._draft_deleter(target.doctype, target.name, token)
        except Exception as exc:  # cleanup must never abort the suite
            outcome["error"] = f"transport error: {type(exc).__name__}"
            return outcome
        outcome["status_code"] = status
        if status is not None and (status in {200, 202, 204} or status == 404):
            outcome["deleted"] = True
        elif status is None:
            outcome["error"] = "transport error"
        else:
            outcome["error"] = f"unexpected status {status}"
        return outcome

    async def _drain_cleanup(self) -> list[dict[str, JsonValue]]:
        outcomes: list[dict[str, JsonValue]] = []
        for target in self._cleanup.pending():
            try:
                outcome = await self._delete_target(target)
            except Exception as exc:  # never raise from cleanup
                outcome = {
                    "doctype": target.doctype,
                    "name": target.name,
                    "deleted": False,
                    "status_code": None,
                    "error": type(exc).__name__,
                }
            if outcome.get("deleted"):
                self._cleanup.forget(doctype=target.doctype, name=target.name)
            outcomes.append(outcome)
        return outcomes

    # -- reporting ----------------------------------------------------------

    def _build_report(
        self,
        suite: EvaluationSuite,
        results: list[EvaluationCaseResult],
    ) -> EvaluationReport:
        passed = sum(result.passed for result in results)
        security_violations = sum(
            not result.passed and result.security_critical for result in results
        )
        pass_rate = passed / len(results) if results else 0.0
        threshold_passed = (
            pass_rate >= suite.thresholds.pass_rate_min
            and security_violations <= suite.thresholds.security_violations_max
        )
        return EvaluationReport(
            suite_id=suite.suite_id,
            suite_schema_version=suite.schema_version,
            execution_mode=suite.execution_mode,
            generated_at=datetime.now(UTC),
            disclaimer=ONLINE_DISCLAIMER,
            thresholds=suite.thresholds,
            summary=EvaluationSummary(
                total=len(results),
                passed=passed,
                failed=len(results) - passed,
                pass_rate=pass_rate,
                security_violations=security_violations,
                threshold_passed=threshold_passed,
                categories=_category_results(results),
            ),
            cases=results,
        )


class OnlineIdentityMissing(RuntimeError):
    def __init__(self, identity: str) -> None:
        super().__init__(f"Required evaluation identity is unavailable: {identity}")
        self.identity = identity


# -- module-level helpers ---------------------------------------------------


def _needs_secondary(suite: EvaluationSuite) -> bool:
    for case in suite.cases:
        if isinstance(case, LiveChatCase) and case.input.identity == "secondary":
            return True
        if isinstance(case, LiveDraftActionCase):
            if case.input.identity == "secondary":
                return True
            if case.expected.cross_user_identity == "secondary":
                return True
    return False


def _needed_identities(suite: EvaluationSuite) -> set[str]:
    needed: set[str] = set()
    for case in suite.cases:
        if isinstance(case, LiveChatCase):
            needed.add(case.input.identity)
        elif isinstance(case, LiveDraftActionCase):
            needed.add(case.input.identity)
            if case.expected.cross_user_identity is not None:
                needed.add(case.expected.cross_user_identity)
    return needed


def _collect_placeholder_texts(suite: EvaluationSuite) -> list[str]:
    texts: list[str] = []
    for case in suite.cases:
        if isinstance(case, LiveChatCase):
            texts.extend(turn.message for turn in case.input.turns)
            texts.extend(case.expected.text_must_contain)
            texts.extend(case.expected.text_must_contain_any)
            texts.extend(case.expected.text_must_not_contain)
        elif isinstance(case, LiveDraftActionCase):
            texts.append(case.input.message)
    return texts


def _date_placeholders() -> dict[str, str]:
    today = datetime.now(UTC).date()
    later = (today + timedelta(days=7)).isoformat()
    return {
        "today": today.isoformat(),
        "delivery_date": later,
        "schedule_date": later,
    }


def _category_results(
    results: list[EvaluationCaseResult],
) -> dict[EvaluationCategory, CategoryResult]:
    from collections import defaultdict

    grouped: dict[EvaluationCategory, list[EvaluationCaseResult]] = defaultdict(list)
    for result in results:
        grouped[result.category].append(result)
    return {
        category: CategoryResult(
            total=len(items),
            passed=sum(item.passed for item in items),
            pass_rate=sum(item.passed for item in items) / len(items),
        )
        for category, items in sorted(grouped.items())
    }
