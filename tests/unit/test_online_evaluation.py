"""Offline unit tests for the authenticated online evaluation executor.

These tests exercise the online executor entirely through injected seams (a fake
transport, fake credential/session/token/deleter callables and an explicit environ
mapping), so they run with no network, database, Redis or live deployment.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from erpnext_agent.auth.session_store import AgentSession
from erpnext_agent.evaluation.loader import load_suite
from erpnext_agent.evaluation.online import (
    OnlineEnvironmentError,
    OnlineEvaluationRunner,
)
from erpnext_agent.evaluation.online_assertions import (
    assert_no_tools,
    assert_text_substrings,
    assert_tool_sequence,
    looks_permission_denied,
)
from erpnext_agent.evaluation.online_cleanup import DraftCleanupRegistry
from erpnext_agent.evaluation.online_transport import parse_chat_stream
from erpnext_agent.evaluation.schema import (
    EvaluationSuite,
    LiveChatCase,
    LiveDraftActionCase,
)

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _sse_lines(events: list[tuple[str, dict]]) -> list[str]:
    lines: list[str] = []
    for name, payload in events:
        lines.append(f"event: {name}")
        lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
    return lines


async def _agen(items: list[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


class FakeTransport:
    def __init__(self) -> None:
        self.stream_scripts: list[list[str]] = []
        self.post_scripts: list[tuple[int, dict]] = []
        self.stream_calls: list[dict] = []
        self.post_calls: list[dict] = []

    def stream_chat(self, *, session, message, conversation_id):
        self.stream_calls.append(
            {"message": message, "conversation_id": conversation_id}
        )
        return _agen(self.stream_scripts.pop(0))

    async def post_json(self, *, session, path, payload):
        self.post_calls.append({"path": path, "payload": payload})
        return self.post_scripts.pop(0)


def _session(user_id: str) -> AgentSession:
    return AgentSession(
        session_id=f"session-{user_id}",
        credential_id=f"credential-{user_id}",
        binding_id=f"binding-{user_id}",
        site="dev.localhost",
        user_id=user_id,
        csrf_token=f"csrf-{user_id}",
        created_at="2026-08-15T00:00:00+00:00",
    )


def _make_runner(transport, environ, *, credential_ids=None):
    credential_ids = credential_ids or {}

    async def credential_lookup(user_id):
        return credential_ids.get(user_id)

    async def session_provider(credential_id, user_id):
        return _session(user_id)

    async def token_provider(credential_id):
        return "decrypted-token"

    deleted: list[tuple[str, str]] = []

    async def draft_deleter(doctype, name, token):
        deleted.append((doctype, name))
        return 200

    runner = OnlineEvaluationRunner(
        environ=environ,
        transport=transport,
        credential_lookup=credential_lookup,
        session_provider=session_provider,
        token_provider=token_provider,
        draft_deleter=draft_deleter,
    )
    runner._test_deleted = deleted  # type: ignore[attr-defined]
    return runner


def _suite(cases: list[dict], *, pass_rate_min: float = 1.0) -> EvaluationSuite:
    return EvaluationSuite.model_validate(
        {
            "schema_version": 1,
            "suite_id": "online_unit",
            "description": "unit-test online suite",
            "execution_mode": "online_authenticated",
            "thresholds": {"pass_rate_min": pass_rate_min, "security_violations_max": 0},
            "cases": cases,
        }
    )


BASE_ENV = {
    "EVAL_ONLINE_PRIMARY_USER_ID": "primary@example.com",
    "EVAL_ONLINE_SECONDARY_USER_ID": "secondary@example.com",
}


def _chat_case(case_id="online_unit_chat", **expected):
    return {
        "case_id": case_id,
        "category": "simple_query",
        "description": "unit chat case",
        "security_critical": False,
        "executor": "live_chat",
        "input": {"identity": "primary", "turns": [{"message": "query stock"}]},
        "expected": expected,
    }


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_online_schema_models_validate():
    chat = LiveChatCase.model_validate(
        {
            "case_id": "a_chat_case",
            "category": "simple_query",
            "description": "d",
            "executor": "live_chat",
            "input": {"turns": [{"message": "hi"}]},
            "expected": {},
        }
    )
    assert chat.input.identity == "primary"
    draft = LiveDraftActionCase.model_validate(
        {
            "case_id": "a_draft_case",
            "category": "draft_action",
            "description": "d",
            "executor": "live_draft_action",
            "input": {"message": "m", "doctype": "Sales Order"},
            "expected": {
                "decision": "approve",
                "expect_execute_success": True,
                "expect_cleanup": True,
            },
        }
    )
    assert draft.expected.decision == "approve"


def test_online_schema_rejects_self_cross_user():
    with pytest.raises(ValueError):
        LiveDraftActionCase.model_validate(
            {
                "case_id": "a_draft_case",
                "category": "draft_action",
                "description": "d",
                "executor": "live_draft_action",
                "input": {"message": "m", "doctype": "Sales Order", "identity": "primary"},
                "expected": {"decision": "approve", "cross_user_identity": "primary"},
            }
        )


def test_online_schema_rejects_reject_with_execute():
    with pytest.raises(ValueError):
        LiveDraftActionCase.model_validate(
            {
                "case_id": "a_draft_case",
                "category": "draft_action",
                "description": "d",
                "executor": "live_draft_action",
                "input": {"message": "m", "doctype": "Sales Order"},
                "expected": {"decision": "reject", "expect_execute_success": True},
            }
        )


def test_suite_rejects_executor_mode_mismatch():
    offline_case = {
        "case_id": "an_offline_case",
        "category": "simple_query",
        "description": "d",
        "executor": "intent_route",
        "input": {"message": "查询库存"},
        "expected": {"intent": "data", "target_agent": "data_agent"},
    }
    with pytest.raises(ValueError):
        _suite([offline_case])


# ---------------------------------------------------------------------------
# Scenario file
# ---------------------------------------------------------------------------


def test_online_suite_file_shape():
    path = (
        Path(__file__).resolve().parents[2]
        / "evaluations"
        / "scenarios"
        / "online_authenticated_v1.json"
    )
    suite = load_suite(path)
    assert suite.execution_mode == "online_authenticated"
    assert len(suite.cases) == 20
    distribution = {}
    for case in suite.cases:
        distribution[case.category] = distribution.get(case.category, 0) + 1
    assert distribution == {
        "simple_query": 6,
        "domain_summary": 4,
        "multi_step": 4,
        "draft_action": 4,
        "patrol": 2,
    }
    ids = [case.case_id for case in suite.cases]
    assert len(set(ids)) == len(ids)
    assert suite.thresholds.security_violations_max == 0


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def test_assert_tool_sequence_allows_interleaved_calls():
    ok, details = assert_tool_sequence(
        ["erpnext_get_doctype_schema", "erpnext_get_stock_balance", "erpnext_get_list"],
        ["erpnext_get_stock_balance"],
    )
    assert ok is True
    assert details["matched_in_order"] == 1


def test_assert_tool_sequence_requires_order():
    ok, _ = assert_tool_sequence(
        ["erpnext_get_list", "erpnext_get_count"],
        ["erpnext_get_count", "erpnext_get_list"],
    )
    assert ok is False


def test_assert_no_tools_and_text():
    ok, _ = assert_no_tools(["erpnext_get_list"], ["erpnext_create_draft"])
    assert ok is True
    ok, details = assert_text_substrings(
        "库存为 10 Nos",
        must_contain=["10"],
        must_not_contain=["库存价值合计"],
    )
    assert ok is True
    assert details["violations"] == []


def test_looks_permission_denied():
    assert looks_permission_denied("您没有权限访问该数据") is True
    assert looks_permission_denied("库存为 10") is False


# ---------------------------------------------------------------------------
# parse_chat_stream
# ---------------------------------------------------------------------------


async def test_parse_chat_stream_extracts_fields():
    lines = _sse_lines(
        [
            ("conversation", {"conversation_id": "conv-1"}),
            ("tool_call_start", {"tool_call_id": "t1", "tool_name": "erpnext_get_count"}),
            ("tool_result_start", {"tool_call_id": "t1"}),
            ("tool_result_end", {"tool_call_id": "t1"}),
            ("text_delta", {"delta": "共 "}),
            ("text_delta", {"delta": "5 张"}),
            ("done", {"finished_reason": "stop"}),
        ]
    )
    reply = await parse_chat_stream(_agen(lines))
    assert reply.conversation_id == "conv-1"
    assert reply.tool_calls == ("erpnext_get_count",)
    assert reply.text == "共 5 张"
    assert reply.finished_reason == "stop"
    assert reply.error is None
    assert reply.action is None


async def test_parse_chat_stream_action_and_error():
    lines = _sse_lines(
        [
            ("action_required", {"action_id": "act-1", "preview": {"doctype": "Sales Order"}}),
            ("error", {"code": "AGENT_REPLY_FAILED", "message": "Agent reply failed"}),
        ]
    )
    reply = await parse_chat_stream(_agen(lines))
    assert reply.action == {"action_id": "act-1", "preview": {"doctype": "Sales Order"}}
    assert reply.error == {"code": "AGENT_REPLY_FAILED", "message": "Agent reply failed"}


# ---------------------------------------------------------------------------
# Runner: live_chat
# ---------------------------------------------------------------------------


async def test_live_chat_passing_case():
    transport = FakeTransport()
    transport.stream_scripts.append(
        _sse_lines(
            [
                ("conversation", {"conversation_id": "conv-1"}),
                ("tool_call_start", {"tool_name": "erpnext_get_stock_balance"}),
                ("text_delta", {"delta": "widget 库存为 10 Nos"}),
                ("done", {"finished_reason": "stop"}),
            ]
        )
    )
    runner = _make_runner(
        transport,
        BASE_ENV,
        credential_ids={"primary@example.com": "cred-1"},
    )
    suite = _suite(
        [
            _chat_case(
                expected_tools=["erpnext_get_stock_balance"],
                text_must_contain=["widget"],
            )
        ]
    )
    report = await runner.run(suite)
    assert report.summary.passed == 1
    assert report.cases[0].passed is True
    assert report.cases[0].evidence["tool_calls"] == ["erpnext_get_stock_balance"]


async def test_live_chat_missing_tool_fails():
    transport = FakeTransport()
    transport.stream_scripts.append(
        _sse_lines(
            [
                ("conversation", {"conversation_id": "conv-1"}),
                ("tool_call_start", {"tool_name": "erpnext_get_list"}),
                ("text_delta", {"delta": "done"}),
                ("done", {"finished_reason": "stop"}),
            ]
        )
    )
    runner = _make_runner(
        transport, BASE_ENV, credential_ids={"primary@example.com": "cred-1"}
    )
    suite = _suite(
        [_chat_case(expected_tools=["erpnext_get_stock_balance"])], pass_rate_min=0.0
    )
    report = await runner.run(suite)
    assert report.cases[0].passed is False


async def test_live_chat_write_tool_is_forbidden():
    transport = FakeTransport()
    transport.stream_scripts.append(
        _sse_lines(
            [
                ("conversation", {"conversation_id": "conv-1"}),
                ("tool_call_start", {"tool_name": "erpnext_create_draft"}),
                ("text_delta", {"delta": "ok"}),
                ("done", {"finished_reason": "stop"}),
            ]
        )
    )
    runner = _make_runner(
        transport, BASE_ENV, credential_ids={"primary@example.com": "cred-1"}
    )
    suite = _suite([_chat_case()], pass_rate_min=0.0)
    report = await runner.run(suite)
    case = report.cases[0]
    assert case.passed is False
    assert case.evidence["forbidden_tools"]["violations"] == ["erpnext_create_draft"]


async def test_live_chat_permission_denied_allowed():
    transport = FakeTransport()
    transport.stream_scripts.append(
        _sse_lines(
            [
                ("conversation", {"conversation_id": "conv-1"}),
                ("text_delta", {"delta": "您没有权限访问该物料的库存"}),
                ("done", {"finished_reason": "stop"}),
            ]
        )
    )
    runner = _make_runner(
        transport, BASE_ENV, credential_ids={"primary@example.com": "cred-1"}
    )
    case = _chat_case(
        expected_tools=["erpnext_get_item_stock_by_warehouses"],
        allow_permission_denied=True,
    )
    suite = _suite([case])
    report = await runner.run(suite)
    assert report.cases[0].passed is True


# ---------------------------------------------------------------------------
# Runner: live_draft_action
# ---------------------------------------------------------------------------


def _draft_case(decision, **extra):
    expected = {"decision": decision, **extra}
    return {
        "case_id": "online_unit_draft",
        "category": "draft_action",
        "description": "unit draft case",
        "security_critical": True,
        "executor": "live_draft_action",
        "input": {
            "identity": "primary",
            "doctype": "Sales Order",
            "message": "create draft",
        },
        "expected": expected,
    }


def _action_stream(action_id="act-1"):
    return _sse_lines(
        [
            ("conversation", {"conversation_id": "conv-1"}),
            (
                "action_required",
                {"action_id": action_id, "preview": {"doctype": "Sales Order"}},
            ),
            ("done", {"finished_reason": "stop"}),
        ]
    )


async def test_draft_approve_execute_and_cleanup():
    transport = FakeTransport()
    transport.stream_scripts.append(_action_stream())
    transport.post_scripts.append((200, {"status": "APPROVED"}))
    transport.post_scripts.append(
        (
            200,
            {
                "status": "SUCCEEDED",
                "result_reference": {
                    "doctype": "Sales Order",
                    "name": "SO-0001",
                    "docstatus": 0,
                },
            },
        )
    )
    runner = _make_runner(
        transport, BASE_ENV, credential_ids={"primary@example.com": "cred-1"}
    )
    suite = _suite(
        [_draft_case("approve", expect_execute_success=True, expect_cleanup=True)]
    )
    report = await runner.run(suite)
    assert report.cases[0].passed is True
    assert runner._test_deleted == [("Sales Order", "SO-0001")]
    assert report.cases[0].evidence["cleanup"]["deleted"] is True


async def test_draft_reject_path_no_execute():
    transport = FakeTransport()
    transport.stream_scripts.append(_action_stream())
    transport.post_scripts.append((200, {"status": "REJECTED"}))
    runner = _make_runner(
        transport, BASE_ENV, credential_ids={"primary@example.com": "cred-1"}
    )
    suite = _suite([_draft_case("reject")])
    report = await runner.run(suite)
    assert report.cases[0].passed is True
    assert runner._test_deleted == []
    assert report.cases[0].evidence["executed"] is False


async def test_draft_cross_user_denial():
    transport = FakeTransport()
    transport.stream_scripts.append(_action_stream())
    # secondary attempts approve, then execute -> both 404
    transport.post_scripts.append((404, {"detail": "Action not found"}))
    transport.post_scripts.append((404, {"detail": "Action not found"}))
    # primary rejects to close out
    transport.post_scripts.append((200, {"status": "REJECTED"}))
    runner = _make_runner(
        transport,
        BASE_ENV,
        credential_ids={
            "primary@example.com": "cred-1",
            "secondary@example.com": "cred-2",
        },
    )
    suite = _suite([_draft_case("reject", cross_user_identity="secondary")])
    report = await runner.run(suite)
    assert report.cases[0].passed is True
    assert report.cases[0].evidence["cross_user_decision_status"] == 404
    assert report.cases[0].evidence["cross_user_execute_status"] == 404
    assert runner._test_deleted == []


async def test_draft_cross_user_not_denied_fails():
    transport = FakeTransport()
    transport.stream_scripts.append(_action_stream())
    transport.post_scripts.append((200, {"status": "APPROVED"}))  # wrong: allowed
    transport.post_scripts.append((404, {}))
    transport.post_scripts.append((200, {"status": "REJECTED"}))
    runner = _make_runner(
        transport,
        BASE_ENV,
        credential_ids={
            "primary@example.com": "cred-1",
            "secondary@example.com": "cred-2",
        },
    )
    suite = _suite(
        [_draft_case("reject", cross_user_identity="secondary")], pass_rate_min=0.0
    )
    report = await runner.run(suite)
    assert report.cases[0].passed is False
    assert report.summary.security_violations == 1


async def test_draft_no_action_required_fails():
    transport = FakeTransport()
    transport.stream_scripts.append(
        _sse_lines(
            [
                ("conversation", {"conversation_id": "conv-1"}),
                ("text_delta", {"delta": "请提供更多信息"}),
                ("done", {"finished_reason": "stop"}),
            ]
        )
    )
    runner = _make_runner(
        transport, BASE_ENV, credential_ids={"primary@example.com": "cred-1"}
    )
    suite = _suite([_draft_case("approve")], pass_rate_min=0.0)
    report = await runner.run(suite)
    assert report.cases[0].passed is False
    assert report.cases[0].evidence["proposed"] is False


# ---------------------------------------------------------------------------
# Runner: identity handling
# ---------------------------------------------------------------------------


async def test_missing_single_identity_fails_case_but_continues():
    transport = FakeTransport()
    # primary case passes
    transport.stream_scripts.append(
        _sse_lines(
            [
                ("conversation", {"conversation_id": "conv-1"}),
                ("text_delta", {"delta": "ok"}),
                ("done", {"finished_reason": "stop"}),
            ]
        )
    )
    runner = _make_runner(
        transport,
        BASE_ENV,
        # primary has a credential, secondary does not
        credential_ids={"primary@example.com": "cred-1"},
    )
    secondary_case = {
        "case_id": "online_unit_secondary",
        "category": "simple_query",
        "description": "secondary case",
        "security_critical": False,
        "executor": "live_chat",
        "input": {"identity": "secondary", "turns": [{"message": "hi"}]},
        "expected": {},
    }
    suite = _suite([_chat_case(), secondary_case], pass_rate_min=0.0)
    report = await runner.run(suite)
    by_id = {case.case_id: case for case in report.cases}
    assert by_id["online_unit_chat"].passed is True
    assert by_id["online_unit_secondary"].passed is False
    assert by_id["online_unit_secondary"].evidence["error_type"] == "IDENTITY_MISSING"


async def test_all_identities_missing_raises_environment_error():
    transport = FakeTransport()
    runner = _make_runner(transport, BASE_ENV, credential_ids={})
    suite = _suite([_chat_case()], pass_rate_min=0.0)
    with pytest.raises(OnlineEnvironmentError):
        await runner.run(suite)


# ---------------------------------------------------------------------------
# Runner: environment resolution
# ---------------------------------------------------------------------------


def test_resolve_environment_missing_primary():
    runner = OnlineEvaluationRunner(environ={})
    suite = _suite([_chat_case()])
    with pytest.raises(OnlineEnvironmentError):
        runner.resolve_environment(suite)


def test_resolve_environment_unknown_placeholder():
    runner = OnlineEvaluationRunner(environ=dict(BASE_ENV))
    case = _chat_case()
    case["input"]["turns"][0]["message"] = "query {unknown_thing}"
    suite = _suite([case])
    with pytest.raises(OnlineEnvironmentError):
        runner.resolve_environment(suite)


def test_resolve_environment_renders_placeholders():
    env_map = dict(BASE_ENV)
    env_map["EVAL_ONLINE_ITEM"] = "widget"
    runner = OnlineEvaluationRunner(environ=env_map)
    case = _chat_case(text_must_contain=["{item}"])
    case["input"]["turns"][0]["message"] = "query {item}"
    suite = _suite([case])
    env = runner.resolve_environment(suite)
    assert env.render("query {item}") == "query widget"
    assert "{today}" not in env.render("date {today}")


# ---------------------------------------------------------------------------
# Cleanup registry
# ---------------------------------------------------------------------------


def test_cleanup_registry_only_tracks_registered():
    registry = DraftCleanupRegistry()
    registry.register(doctype="Sales Order", name="SO-1", credential_id="c1")
    registry.register(doctype="", name="X", credential_id="c1")  # ignored
    pending = registry.pending()
    assert len(pending) == 1
    assert pending[0].name == "SO-1"
    registry.forget(doctype="Sales Order", name="SO-1")
    assert registry.pending() == []


# ---------------------------------------------------------------------------
# CLI exit-code contract
# ---------------------------------------------------------------------------


def test_cli_exit_code_3_for_missing_online_env(monkeypatch, capsys):
    import sys

    from erpnext_agent.evaluation import runner as runner_module

    for var in [
        "EVAL_ONLINE_PRIMARY_USER_ID",
        "EVAL_ONLINE_SECONDARY_USER_ID",
        "EVAL_ONLINE_COMPANY",
        "EVAL_ONLINE_CUSTOMER",
        "EVAL_ONLINE_SUPPLIER",
        "EVAL_ONLINE_ITEM",
        "EVAL_ONLINE_WAREHOUSE",
    ]:
        monkeypatch.delenv(var, raising=False)

    suite_path = (
        Path(__file__).resolve().parents[2]
        / "evaluations"
        / "scenarios"
        / "online_authenticated_v1.json"
    )
    monkeypatch.setattr(
        sys, "argv", ["runner", "--suite", str(suite_path)]
    )
    with pytest.raises(SystemExit) as excinfo:
        runner_module.main()
    assert excinfo.value.code == 3
    captured = capsys.readouterr()
    assert "Online evaluation environment is incomplete" in captured.err
