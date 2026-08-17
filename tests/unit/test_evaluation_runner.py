from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from erpnext_agent.evaluation.loader import EvaluationSuiteError, load_suite
from erpnext_agent.evaluation.runner import EvaluationRunner, main, report_markdown
from erpnext_agent.evaluation.schema import (
    CategoryResult,
    EvaluationCaseResult,
    EvaluationReport,
    EvaluationSuite,
    EvaluationSummary,
    SuiteThresholds,
)

PROJECT_ROOT = Path(__file__).parents[2]
SUITE_PATH = PROJECT_ROOT / "evaluations/scenarios/offline_policy_v1.json"
ONLINE_SUITE_PATH = PROJECT_ROOT / "evaluations/scenarios/online_authenticated_v1.json"


def test_offline_suite_is_versioned_unique_and_has_first_twenty_cases() -> None:
    suite = load_suite(SUITE_PATH)

    assert suite.schema_version == 1
    assert suite.execution_mode == "offline_deterministic"
    assert len(suite.cases) == 20
    assert len({case.case_id for case in suite.cases}) == len(suite.cases)
    assert sum(case.security_critical for case in suite.cases) == 13
    assert {
        category: sum(case.category == category for case in suite.cases)
        for category in {
            "simple_query",
            "domain_summary",
            "multi_step",
            "draft_action",
            "patrol",
            "security_negative",
        }
    } == {
        "simple_query": 2,
        "domain_summary": 2,
        "multi_step": 2,
        "draft_action": 4,
        "patrol": 2,
        "security_negative": 8,
    }


def test_offline_suite_passes_its_declared_thresholds() -> None:
    report = EvaluationRunner().run(load_suite(SUITE_PATH))

    assert report.summary.total == 20
    assert report.summary.passed == 20
    assert report.summary.failed == 0
    assert report.summary.pass_rate == 1.0
    assert report.summary.security_violations == 0
    assert report.summary.threshold_passed is True
    assert set(report.summary.categories) == {
        "simple_query",
        "domain_summary",
        "multi_step",
        "draft_action",
        "patrol",
        "security_negative",
    }
    assert "do not measure live model quality" in report.disclaimer


def test_report_contains_per_case_evidence_and_markdown_disclaimer() -> None:
    report = EvaluationRunner().run(load_suite(SUITE_PATH))
    markdown = report_markdown(report)

    assert all(result.evidence for result in report.cases)
    assert "Status: **PASS**" in markdown
    assert "offline_deterministic" in markdown
    assert "do not measure live model quality" in markdown
    assert "deny_submit_request" in markdown


def test_suite_rejects_duplicate_case_ids() -> None:
    payload = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    payload["cases"][1]["case_id"] = payload["cases"][0]["case_id"]

    with pytest.raises(ValueError, match="duplicate evaluation case IDs"):
        EvaluationSuite.model_validate(payload)


def test_suite_rejects_unknown_fields(tmp_path: Path) -> None:
    payload = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    payload["unexpected"] = "must fail closed"
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationSuiteError, match="Evaluation suite is invalid"):
        load_suite(invalid_path)


def test_failed_critical_case_fails_security_threshold() -> None:
    payload = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    only_case = next(
        case for case in payload["cases"] if case["case_id"] == "deny_submit_request"
    )
    only_case["expected"] = {"intent": "data", "target_agent": "data_agent"}
    payload["cases"] = [only_case]
    payload["thresholds"]["pass_rate_min"] = 0.0
    suite = EvaluationSuite.model_validate(payload)

    report = EvaluationRunner().run(suite)

    assert report.summary.pass_rate == 0.0
    assert report.summary.security_violations == 1
    assert report.summary.threshold_passed is False


def test_cli_exit_codes_distinguish_pass_failure_and_invalid_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["evaluation-runner", "--suite", str(SUITE_PATH)])
    with pytest.raises(SystemExit) as passed_exit:
        main()
    assert passed_exit.value.code == 0
    assert '"threshold_passed": true' in capsys.readouterr().out

    payload = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    payload["cases"][0]["expected"] = {
        "intent": "patrol",
        "target_agent": "patrol_agent",
    }
    failed_path = tmp_path / "failed.json"
    failed_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["evaluation-runner", "--suite", str(failed_path)])
    with pytest.raises(SystemExit) as failed_exit:
        main()
    assert failed_exit.value.code == 1
    assert '"threshold_passed": false' in capsys.readouterr().out

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["evaluation-runner", "--suite", str(invalid_path)])
    with pytest.raises(SystemExit) as invalid_exit:
        main()
    assert invalid_exit.value.code == 2
    assert "Evaluation suite is invalid" in capsys.readouterr().err


def _online_report(threshold_passed: bool) -> EvaluationReport:
    from datetime import UTC, datetime

    passed = 1 if threshold_passed else 0
    case = EvaluationCaseResult(
        case_id="c1",
        category="simple_query",
        executor="live_chat",
        security_critical=False,
        passed=threshold_passed,
        duration_ms=1.0,
        evidence={},
    )
    summary = EvaluationSummary(
        total=1,
        passed=passed,
        failed=1 - passed,
        pass_rate=1.0 if threshold_passed else 0.0,
        security_violations=0,
        threshold_passed=threshold_passed,
        categories={
            "simple_query": CategoryResult(
                total=1,
                passed=passed,
                pass_rate=1.0 if threshold_passed else 0.0,
            )
        },
    )
    return EvaluationReport(
        suite_id="online_unit",
        suite_schema_version=1,
        execution_mode="online_authenticated",
        generated_at=datetime.now(UTC),
        disclaimer="d",
        thresholds=SuiteThresholds(pass_rate_min=0.8),
        summary=summary,
        cases=[case],
    )


def _patch_online_runner(
    monkeypatch: pytest.MonkeyPatch,
    reports: list[EvaluationReport],
) -> None:
    import erpnext_agent.evaluation.online as online_module

    class FakeRunner:
        async def run(self, suite: EvaluationSuite) -> EvaluationReport:
            del suite
            return reports.pop(0)

    monkeypatch.setattr(online_module, "OnlineEvaluationRunner", FakeRunner)


def test_repeat_flag_accepted_for_offline_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    json_report = tmp_path / "offline.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--suite", str(SUITE_PATH), "--repeat", "3", "--json-report", str(json_report)],
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 0
    # Offline suites ignore --repeat and write a single plain report.
    assert json_report.exists()


def test_repeat_zero_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "argv", ["runner", "--suite", str(SUITE_PATH), "--repeat", "0"]
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2


def test_repeat_single_writes_plain_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_online_runner(monkeypatch, [_online_report(True)])
    json_report = tmp_path / "rep.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--suite", str(ONLINE_SUITE_PATH), "--json-report", str(json_report)],
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 0
    assert json_report.exists()
    assert not (tmp_path / "rep_run1.json").exists()


def test_repeat_exit_code_worst_run_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_online_runner(monkeypatch, [_online_report(True), _online_report(False)])
    json_report = tmp_path / "rep.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--suite",
            str(ONLINE_SUITE_PATH),
            "--repeat",
            "2",
            "--json-report",
            str(json_report),
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    # One run failed its threshold, so the whole gate fails.
    assert exit_info.value.code == 1
    assert (tmp_path / "rep_run1.json").exists()
    assert (tmp_path / "rep_run2.json").exists()
    assert not json_report.exists()
