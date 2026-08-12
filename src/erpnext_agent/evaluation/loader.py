from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from erpnext_agent.evaluation.schema import EvaluationSuite


class EvaluationSuiteError(ValueError):
    """A safe scenario loading error with no environment or credential content."""


def load_suite(path: Path) -> EvaluationSuite:
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvaluationSuiteError(f"Unable to read evaluation suite: {path}") from exc
    try:
        return EvaluationSuite.model_validate_json(payload)
    except ValidationError as exc:
        raise EvaluationSuiteError(f"Evaluation suite is invalid: {path}\n{exc}") from exc
