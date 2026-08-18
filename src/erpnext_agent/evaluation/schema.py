from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from erpnext_agent.agents.orchestrator import Intent

EvaluationCategory = Literal[
    "simple_query",
    "domain_summary",
    "multi_step",
    "draft_action",
    "patrol",
    "security_negative",
]
AgentName = Literal["data_agent", "action_agent", "patrol_agent", "orchestrator"]
ExecutionMode = Literal["offline_deterministic", "online_authenticated"]
OnlineIdentity = Literal["primary", "secondary"]
WritableDoctype = Literal["Sales Order", "Purchase Order", "Material Request"]
OFFLINE_EXECUTORS = frozenset({"intent_route", "tool_policy", "action_validation", "mcp_envelope"})
ONLINE_EXECUTORS = frozenset({"live_chat", "live_draft_action"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SuiteThresholds(StrictModel):
    pass_rate_min: float = Field(ge=0, le=1)
    security_violations_max: int = Field(default=0, ge=0)


class IntentRouteInput(StrictModel):
    message: str = Field(min_length=1, max_length=16_000)
    previous_user_messages: list[str] = Field(default_factory=list, max_length=20)


class IntentRouteExpected(StrictModel):
    intent: Intent
    target_agent: str | None


class ToolPolicyInput(StrictModel):
    agent_name: AgentName
    tool_name: str = Field(min_length=1, max_length=128)


class ToolPolicyExpected(StrictModel):
    allowed: bool


class ActionValidationInput(StrictModel):
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, JsonValue]


class ActionValidationExpected(StrictModel):
    accepted: bool
    error_code: str | None = None

    @model_validator(mode="after")
    def require_error_code_for_rejection(self) -> ActionValidationExpected:
        if self.accepted and self.error_code is not None:
            raise ValueError("accepted action cases cannot expect an error_code")
        if not self.accepted and not self.error_code:
            raise ValueError("rejected action cases must expect an error_code")
        return self


class MCPEnvelopeInput(StrictModel):
    payload: dict[str, JsonValue]


class MCPEnvelopeExpected(StrictModel):
    accepted: bool
    error_type: Literal[
        "MCPProtocolError",
        "MCPToolError",
        "MCPBusinessError",
        "MCPContractError",
    ] | None = None
    error_code: str | None = None
    content_trust: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> MCPEnvelopeExpected:
        if self.accepted and (self.error_type is not None or self.error_code is not None):
            raise ValueError("accepted MCP cases cannot expect an error")
        if not self.accepted and (self.error_type is None or self.error_code is None):
            raise ValueError("rejected MCP cases require error_type and error_code")
        return self


class CaseBase(StrictModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    category: EvaluationCategory
    description: str = Field(min_length=1, max_length=500)
    security_critical: bool = False


class IntentRouteCase(CaseBase):
    executor: Literal["intent_route"]
    input: IntentRouteInput
    expected: IntentRouteExpected


class ToolPolicyCase(CaseBase):
    executor: Literal["tool_policy"]
    input: ToolPolicyInput
    expected: ToolPolicyExpected


class ActionValidationCase(CaseBase):
    executor: Literal["action_validation"]
    input: ActionValidationInput
    expected: ActionValidationExpected


class MCPEnvelopeCase(CaseBase):
    executor: Literal["mcp_envelope"]
    input: MCPEnvelopeInput
    expected: MCPEnvelopeExpected


class LiveChatTurn(StrictModel):
    message: str = Field(min_length=1, max_length=16_000)
    expected_tools: list[str] = Field(default_factory=list, max_length=20)
    expected_route: str | None = Field(default=None, max_length=64)


class LiveChatInput(StrictModel):
    identity: OnlineIdentity = "primary"
    turns: list[LiveChatTurn] = Field(min_length=1, max_length=5)


class LiveChatExpected(StrictModel):
    expected_tools: list[str] = Field(default_factory=list, max_length=20)
    forbidden_tools: list[str] = Field(default_factory=list, max_length=20)
    text_must_contain: list[str] = Field(default_factory=list, max_length=20)
    text_must_contain_any: list[str] = Field(default_factory=list, max_length=20)
    text_must_not_contain: list[str] = Field(default_factory=list, max_length=20)
    optional_env_facts: list[str] = Field(default_factory=list, max_length=10)
    allow_permission_denied: bool = False
    expect_error: bool = False


class LiveChatCase(CaseBase):
    executor: Literal["live_chat"]
    input: LiveChatInput
    expected: LiveChatExpected


class LiveDraftActionInput(StrictModel):
    identity: OnlineIdentity = "primary"
    message: str = Field(min_length=1, max_length=16_000)
    doctype: WritableDoctype


class LiveDraftActionExpected(StrictModel):
    decision: Literal["approve", "reject"]
    cross_user_identity: OnlineIdentity | None = None
    expect_execute_success: bool = False
    expect_cleanup: bool = False

    @model_validator(mode="after")
    def validate_flow(self) -> LiveDraftActionExpected:
        if self.decision == "reject" and (self.expect_execute_success or self.expect_cleanup):
            raise ValueError("rejected draft cases cannot expect execution or cleanup")
        return self


class LiveDraftActionCase(CaseBase):
    executor: Literal["live_draft_action"]
    input: LiveDraftActionInput
    expected: LiveDraftActionExpected

    @model_validator(mode="after")
    def cross_user_differs_from_owner(self) -> LiveDraftActionCase:
        if (
            self.expected.cross_user_identity is not None
            and self.expected.cross_user_identity == self.input.identity
        ):
            raise ValueError("cross_user_identity must differ from the case identity")
        return self


EvaluationCase = Annotated[
    IntentRouteCase
    | ToolPolicyCase
    | ActionValidationCase
    | MCPEnvelopeCase
    | LiveChatCase
    | LiveDraftActionCase,
    Field(discriminator="executor"),
]


class EvaluationSuite(StrictModel):
    schema_version: Literal[1]
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    description: str = Field(min_length=1, max_length=1000)
    execution_mode: ExecutionMode
    thresholds: SuiteThresholds
    cases: list[EvaluationCase] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> EvaluationSuite:
        case_ids = [case.case_id for case in self.cases]
        duplicates = sorted(
            case_id for case_id in set(case_ids) if case_ids.count(case_id) > 1
        )
        if duplicates:
            raise ValueError(f"duplicate evaluation case IDs: {duplicates}")
        return self

    @model_validator(mode="after")
    def executors_match_execution_mode(self) -> EvaluationSuite:
        allowed = (
            OFFLINE_EXECUTORS
            if self.execution_mode == "offline_deterministic"
            else ONLINE_EXECUTORS
        )
        mismatched = sorted(
            case.case_id for case in self.cases if case.executor not in allowed
        )
        if mismatched:
            raise ValueError(
                f"cases use executors that do not match execution_mode "
                f"{self.execution_mode}: {mismatched}"
            )
        return self


class EvaluationCaseResult(StrictModel):
    case_id: str
    category: EvaluationCategory
    executor: str
    security_critical: bool
    passed: bool
    duration_ms: float = Field(ge=0)
    evidence: dict[str, JsonValue]


class CategoryResult(StrictModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)


class EvaluationSummary(StrictModel):
    total: int = Field(ge=1)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    security_violations: int = Field(ge=0)
    threshold_passed: bool
    categories: dict[EvaluationCategory, CategoryResult]


class EvaluationReport(StrictModel):
    report_schema_version: Literal[1] = 1
    suite_id: str
    suite_schema_version: int
    execution_mode: ExecutionMode
    generated_at: datetime
    disclaimer: str
    thresholds: SuiteThresholds
    summary: EvaluationSummary
    cases: list[EvaluationCaseResult]
