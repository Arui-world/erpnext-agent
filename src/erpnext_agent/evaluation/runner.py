from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from erpnext_agent.actions.proposal import (
    ActionProposalError,
    validate_action_arguments,
)
from erpnext_agent.agents.orchestrator import IntentGate
from erpnext_agent.evaluation.loader import EvaluationSuiteError, load_suite
from erpnext_agent.evaluation.schema import (
    ActionValidationCase,
    CategoryResult,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationCategory,
    EvaluationReport,
    EvaluationSuite,
    EvaluationSummary,
    IntentRouteCase,
    MCPEnvelopeCase,
    ToolPolicyCase,
)
from erpnext_agent.mcp.adapter import MCPError, normalize_tool_response
from erpnext_agent.mcp.policy import ToolPolicyError, assert_tool_allowed

DISCLAIMER = (
    "Offline deterministic policy evaluation only. These results do not measure live model "
    "quality, ERPNext data accuracy, OAuth permission differences, or end-to-end latency."
)


class EvaluationRunner:
    def run(self, suite: EvaluationSuite) -> EvaluationReport:
        results = [self._execute(case) for case in suite.cases]
        passed = sum(result.passed for result in results)
        security_violations = sum(
            not result.passed and result.security_critical for result in results
        )
        pass_rate = passed / len(results)
        categories = self._category_results(results)
        threshold_passed = (
            pass_rate >= suite.thresholds.pass_rate_min
            and security_violations <= suite.thresholds.security_violations_max
        )
        return EvaluationReport(
            suite_id=suite.suite_id,
            suite_schema_version=suite.schema_version,
            execution_mode=suite.execution_mode,
            generated_at=datetime.now(UTC),
            disclaimer=DISCLAIMER,
            thresholds=suite.thresholds,
            summary=EvaluationSummary(
                total=len(results),
                passed=passed,
                failed=len(results) - passed,
                pass_rate=pass_rate,
                security_violations=security_violations,
                threshold_passed=threshold_passed,
                categories=categories,
            ),
            cases=results,
        )

    def _execute(self, case: EvaluationCase) -> EvaluationCaseResult:
        started = time.perf_counter()
        try:
            if isinstance(case, IntentRouteCase):
                passed, evidence = self._intent_route(case)
            elif isinstance(case, ToolPolicyCase):
                passed, evidence = self._tool_policy(case)
            elif isinstance(case, ActionValidationCase):
                passed, evidence = self._action_validation(case)
            elif isinstance(case, MCPEnvelopeCase):
                passed, evidence = self._mcp_envelope(case)
            else:
                passed = False
                evidence = {"error_type": "UNSUPPORTED_EXECUTOR"}
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

    @staticmethod
    def _intent_route(case: IntentRouteCase) -> tuple[bool, dict[str, JsonValue]]:
        decision = IntentGate().route_with_context(
            case.input.message,
            case.input.previous_user_messages,
        )
        actual: dict[str, JsonValue] = {
            "intent": decision.intent.value,
            "target_agent": decision.target_agent,
        }
        expected: dict[str, JsonValue] = {
            "intent": case.expected.intent.value,
            "target_agent": case.expected.target_agent,
        }
        return actual == expected, {"expected": expected, "actual": actual}

    @staticmethod
    def _tool_policy(case: ToolPolicyCase) -> tuple[bool, dict[str, JsonValue]]:
        try:
            assert_tool_allowed(case.input.agent_name, case.input.tool_name)
            allowed = True
        except ToolPolicyError:
            allowed = False
        return allowed == case.expected.allowed, {
            "expected_allowed": case.expected.allowed,
            "actual_allowed": allowed,
            "agent_name": case.input.agent_name,
            "tool_name": case.input.tool_name,
        }

    @staticmethod
    def _action_validation(
        case: ActionValidationCase,
    ) -> tuple[bool, dict[str, JsonValue]]:
        accepted = False
        error_code: str | None = None
        normalized_doctype: str | None = None
        try:
            arguments = cast(dict[str, object], case.input.arguments)
            normalized = validate_action_arguments(case.input.tool_name, arguments)
            accepted = True
            normalized_doctype = str(normalized.get("doctype"))
        except ActionProposalError as exc:
            error_code = exc.code
        passed = (
            accepted == case.expected.accepted
            and error_code == case.expected.error_code
        )
        return passed, {
            "expected_accepted": case.expected.accepted,
            "actual_accepted": accepted,
            "expected_error_code": case.expected.error_code,
            "actual_error_code": error_code,
            "normalized_doctype": normalized_doctype,
        }

    @staticmethod
    def _mcp_envelope(case: MCPEnvelopeCase) -> tuple[bool, dict[str, JsonValue]]:
        accepted = False
        error_type: str | None = None
        error_code: str | None = None
        content_trust: str | None = None
        try:
            envelope = normalize_tool_response(cast(dict[str, object], case.input.payload))
            accepted = True
            content_trust = envelope.content_trust
        except MCPError as exc:
            error_type = type(exc).__name__
            error_code = exc.code
        passed = (
            accepted == case.expected.accepted
            and error_type == case.expected.error_type
            and error_code == case.expected.error_code
            and content_trust == case.expected.content_trust
        )
        return passed, {
            "expected_accepted": case.expected.accepted,
            "actual_accepted": accepted,
            "expected_error_type": case.expected.error_type,
            "actual_error_type": error_type,
            "expected_error_code": case.expected.error_code,
            "actual_error_code": error_code,
            "expected_content_trust": case.expected.content_trust,
            "actual_content_trust": content_trust,
        }

    @staticmethod
    def _category_results(
        results: list[EvaluationCaseResult],
    ) -> dict[EvaluationCategory, CategoryResult]:
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


def report_markdown(report: EvaluationReport) -> str:
    status = "PASS" if report.summary.threshold_passed else "FAIL"
    lines = [
        f"# Evaluation report: {report.suite_id}",
        "",
        f"- Status: **{status}**",
        f"- Generated: `{report.generated_at.isoformat()}`",
        f"- Execution mode: `{report.execution_mode}`",
        f"- Passed: `{report.summary.passed}/{report.summary.total}`",
        f"- Pass rate: `{report.summary.pass_rate:.2%}`",
        f"- Security violations: `{report.summary.security_violations}`",
        "",
        f"> {report.disclaimer}",
        "",
        "## Category results",
        "",
        "| Category | Passed | Total | Pass rate |",
        "|---|---:|---:|---:|",
    ]
    for category, category_result in report.summary.categories.items():
        lines.append(
            f"| {category} | {category_result.passed} | {category_result.total} | "
            f"{category_result.pass_rate:.2%} |"
        )
    lines.extend(
        [
            "",
            "## Cases",
            "",
            "| Case | Executor | Category | Critical | Result |",
            "|---|---|---|---:|---|",
        ]
    )
    for case_result in report.cases:
        lines.append(
            f"| {case_result.case_id} | {case_result.executor} | "
            f"{case_result.category} | "
            f"{'yes' if case_result.security_critical else 'no'} | "
            f"{'PASS' if case_result.passed else 'FAIL'} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def default_suite_path() -> Path:
    return Path(__file__).resolve().parents[3] / "evaluations/scenarios/offline_policy_v1.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run versioned ERPNext Agent evaluations")
    parser.add_argument("--suite", type=Path, default=default_suite_path())
    parser.add_argument("--json-report", type=Path)
    parser.add_argument("--markdown-report", type=Path)
    arguments = parser.parse_args()
    try:
        suite = load_suite(arguments.suite)
    except EvaluationSuiteError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    if suite.execution_mode == "online_authenticated":
        report = _run_online_suite(suite)
    else:
        report = EvaluationRunner().run(suite)
    json_content = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if arguments.json_report:
        write_report(arguments.json_report, json_content)
    if arguments.markdown_report:
        write_report(arguments.markdown_report, report_markdown(report))
    print(json_content, end="")
    raise SystemExit(0 if report.summary.threshold_passed else 1)


def _run_online_suite(suite: EvaluationSuite) -> EvaluationReport:
    # Imported lazily so the offline path never pulls in httpx / redis / sqlalchemy.
    import asyncio

    from erpnext_agent.evaluation.online import (
        OnlineEnvironmentError,
        OnlineEvaluationRunner,
    )

    try:
        return asyncio.run(OnlineEvaluationRunner().run(suite))
    except OnlineEnvironmentError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from exc


if __name__ == "__main__":
    main()
