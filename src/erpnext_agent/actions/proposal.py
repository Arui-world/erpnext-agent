from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk
from opentelemetry.trace import Span
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.actions.gateway import ActionGateway
from erpnext_agent.actions.models import ActionRecord
from erpnext_agent.mcp.adapter import MCPContractError, MCPError
from erpnext_agent.mcp.tool_bridge import MCPToolCaller
from erpnext_agent.observability import Telemetry, set_span_result

logger = logging.getLogger(__name__)

CREATE_DRAFT_TOOL = "erpnext_create_draft"
UPDATE_DRAFT_TOOL = "erpnext_update_draft"
PROPOSE_DRAFT_TOOL = "erpnext_propose_draft_action"


@dataclass(frozen=True, slots=True)
class DraftDefinition:
    required_fields: frozenset[str]
    allowed_fields: frozenset[str]
    allowed_item_fields: frozenset[str]


_COMMON_ITEM_FIELDS = frozenset(
    {
        "item_code",
        "item_name",
        "description",
        "qty",
        "uom",
        "conversion_factor",
        "warehouse",
    }
)

DRAFT_DEFINITIONS: dict[str, DraftDefinition] = {
    "Sales Order": DraftDefinition(
        required_fields=frozenset(
            {"customer", "company", "transaction_date", "delivery_date", "items"}
        ),
        allowed_fields=frozenset(
            {
                "customer",
                "company",
                "transaction_date",
                "delivery_date",
                "currency",
                "selling_price_list",
                "po_no",
                "items",
            }
        ),
        allowed_item_fields=_COMMON_ITEM_FIELDS | {"rate", "delivery_date"},
    ),
    "Purchase Order": DraftDefinition(
        required_fields=frozenset(
            {"supplier", "company", "transaction_date", "schedule_date", "items"}
        ),
        allowed_fields=frozenset(
            {
                "supplier",
                "company",
                "transaction_date",
                "schedule_date",
                "currency",
                "buying_price_list",
                "items",
            }
        ),
        allowed_item_fields=_COMMON_ITEM_FIELDS | {"rate", "schedule_date"},
    ),
    "Material Request": DraftDefinition(
        required_fields=frozenset(
            {
                "material_request_type",
                "company",
                "transaction_date",
                "schedule_date",
                "items",
            }
        ),
        allowed_fields=frozenset(
            {
                "material_request_type",
                "company",
                "transaction_date",
                "schedule_date",
                "set_warehouse",
                "items",
            }
        ),
        allowed_item_fields=_COMMON_ITEM_FIELDS | {"schedule_date"},
    ),
}

_DATE_FIELDS = frozenset({"transaction_date", "delivery_date", "schedule_date"})
_LINK_FIELDS: dict[str, str] = {
    "company": "Company",
    "customer": "Customer",
    "supplier": "Supplier",
    "item_code": "Item",
    "warehouse": "Warehouse",
    "set_warehouse": "Warehouse",
}


class ActionProposalError(ValueError):
    def __init__(self, message: str, *, code: str = "INVALID_ACTION_PROPOSAL") -> None:
        super().__init__(message)
        self.code = code


def validate_action_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate the local write policy before an approval record can exist."""

    allowed_top_level = (
        {"doctype", "payload"}
        if tool_name == CREATE_DRAFT_TOOL
        else {"doctype", "name", "payload", "expected_modified"}
        if tool_name == UPDATE_DRAFT_TOOL
        else None
    )
    if allowed_top_level is None:
        raise ActionProposalError("Only draft create and update operations are supported")
    unexpected = set(arguments) - allowed_top_level
    missing = allowed_top_level - set(arguments)
    if unexpected:
        raise ActionProposalError(f"Unexpected action arguments: {sorted(unexpected)}")
    if missing:
        raise ActionProposalError(f"Missing action arguments: {sorted(missing)}")

    doctype = _required_string(arguments.get("doctype"), "doctype", max_length=180)
    definition = DRAFT_DEFINITIONS.get(doctype)
    if definition is None:
        raise ActionProposalError(f"Unsupported draft DocType: {doctype}")
    payload = arguments.get("payload")
    if not isinstance(payload, dict) or not payload:
        raise ActionProposalError("payload must be a non-empty object")
    if len(payload) > 30:
        raise ActionProposalError("payload exceeds the 30-field limit")

    unexpected_fields = set(payload) - definition.allowed_fields
    if unexpected_fields:
        raise ActionProposalError(f"Payload fields are not allowed: {sorted(unexpected_fields)}")
    if tool_name == CREATE_DRAFT_TOOL:
        missing_fields = definition.required_fields - set(payload)
        if missing_fields:
            raise ActionProposalError(
                f"Missing required {doctype} fields: {sorted(missing_fields)}",
                code="MISSING_REQUIRED_FIELDS",
            )

    normalized_payload = _normalize_payload(payload, definition)
    normalized: dict[str, Any] = {"doctype": doctype, "payload": normalized_payload}
    if tool_name == UPDATE_DRAFT_TOOL:
        normalized["name"] = _required_string(arguments.get("name"), "name", max_length=180)
        normalized["expected_modified"] = _required_string(
            arguments.get("expected_modified"),
            "expected_modified",
            max_length=128,
        )
    return normalized


def build_action_preview(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    payload = dict(arguments["payload"])
    items = payload.pop("items", [])
    operation = "create" if tool_name == CREATE_DRAFT_TOOL else "update"
    preview: dict[str, Any] = {
        "operation": operation,
        "title": f"{'创建' if operation == 'create' else '修改'} {arguments['doctype']} 草稿",
        "doctype": arguments["doctype"],
        "document_name": arguments.get("name"),
        "fields": payload,
        "items": items,
        "item_count": len(items) if isinstance(items, list) else 0,
        "expected_modified": arguments.get("expected_modified"),
        "notice": "批准后只保存 docstatus=0 草稿，不会提交、过账或预占库存。",
    }
    if conversation_id is not None:
        preview["conversation_id"] = conversation_id
    return preview


def action_public_payload(record: ActionRecord) -> dict[str, Any]:
    return {
        "action_id": record.action_id,
        "tool_name": record.tool_name,
        "arguments_sha256": record.arguments_sha256,
        "preview": record.preview,
        "status": record.status,
        "created_at": record.created_at.isoformat(),
        "expires_at": record.expires_at.isoformat(),
        "decided_at": record.decided_at.isoformat() if record.decided_at else None,
        "executed_at": record.executed_at.isoformat() if record.executed_at else None,
        "mcp_trace_id": record.mcp_trace_id,
        "result_reference": record.result_reference,
        "failure_code": record.failure_code,
        "failure_message": record.failure_message,
    }


def action_summary_markdown(record: ActionRecord) -> str:
    title = str(record.preview.get("title") or "ERPNext 草稿操作")
    return (
        "\n\n---\n"
        f"**待审批 Action：{title}**\n\n"
        f"- Action ID：`{record.action_id}`\n"
        f"- 当前状态：`{record.status}`\n"
        f"- 过期时间：`{record.expires_at.isoformat()}`\n"
        "- 批准后仅保存草稿（`docstatus=0`），不会提交或过账。"
    )


class ActionProposalService:
    def __init__(self, gateway: ActionGateway, caller: MCPToolCaller) -> None:
        self._gateway = gateway
        self._caller = caller

    async def propose(
        self,
        session: AsyncSession,
        *,
        session_id: str,
        site: str,
        requested_by: str,
        access_token: str,
        tool_name: str,
        arguments: dict[str, Any],
        conversation_id: str | None = None,
    ) -> ActionRecord:
        normalized = validate_action_arguments(tool_name, arguments)
        await self._validate_schema(access_token, normalized)
        await self._resolve_warehouse_alias(access_token, normalized)
        source_versions = await self._validate_current_document(
            access_token,
            tool_name,
            normalized,
        )
        await self._validate_links(access_token, normalized)
        record = await self._gateway.create_pending(
            session,
            session_id=session_id,
            site=site,
            requested_by=requested_by,
            tool_name=tool_name,
            arguments=normalized,
            preview=build_action_preview(
                tool_name,
                normalized,
                conversation_id=conversation_id,
            ),
            source_versions=source_versions,
        )
        await session.commit()
        return record

    async def _resolve_warehouse_alias(
        self,
        access_token: str,
        arguments: dict[str, Any],
    ) -> None:
        """Expand a bare warehouse label using the current user's company abbreviation."""
        payload = arguments["payload"]
        values: list[tuple[dict[str, Any], str]] = []
        for row in payload.get("items", []):
            if isinstance(row, dict) and isinstance(row.get("warehouse"), str):
                values.append((row, "warehouse"))
        if isinstance(payload.get("set_warehouse"), str):
            values.append((payload, "set_warehouse"))
        if not values:
            return

        unresolved: list[tuple[dict[str, Any], str, str]] = []
        for target, field in values:
            value = target[field].strip()
            exact = await self._caller.call_tool(
                access_token=access_token,
                name="erpnext_get_list",
                arguments={
                    "doctype": "Warehouse", "fields": ["name"],
                    "filters": {"name": value}, "limit_start": 0, "limit_page_length": 2,
                },
            )
            rows = exact.data.get("rows", []) if isinstance(exact.data, dict) else []
            if not any(isinstance(row, dict) and row.get("name") == value for row in rows):
                unresolved.append((target, field, value))
        if not unresolved:
            return
        try:
            context = await self._caller.call_tool(
                access_token=access_token, name="erpnext_get_user_business_context", arguments={}
            )
        except (MCPError, MCPContractError, AssertionError):
            context = None
        data = context.data if context is not None else None
        abbr = data.get("company_abbr") if isinstance(data, dict) else None
        if not isinstance(abbr, str) or not abbr.strip():
            company = payload.get("company")
            if isinstance(company, str) and company.strip():
                try:
                    company_result = await self._caller.call_tool(
                        access_token=access_token,
                        name="erpnext_get_list",
                        arguments={
                            "doctype": "Company", "fields": ["name", "abbr"],
                            "filters": {"name": company.strip()}, "limit_start": 0, "limit_page_length": 2,
                        },
                    )
                    company_rows = company_result.data.get("rows", []) if isinstance(company_result.data, dict) else []
                    abbr = company_rows[0].get("abbr") if len(company_rows) == 1 else None
                except (MCPError, MCPContractError, AssertionError):
                    abbr = None
        if not isinstance(abbr, str) or not abbr.strip():
            return
        for target, field, value in unresolved:
            candidate = f"{value} - {abbr.strip()}"
            result = await self._caller.call_tool(
                access_token=access_token,
                name="erpnext_get_list",
                arguments={
                    "doctype": "Warehouse",
                    "fields": ["name"],
                    "filters": {"name": ["like", candidate + "%"]},
                    "limit_start": 0,
                    "limit_page_length": 2,
                },
            )
            candidate_rows = result.data.get("rows", []) if isinstance(result.data, dict) else []
            if len(candidate_rows) == 1:
                target[field] = candidate_rows[0].get("name", candidate)

    async def _validate_schema(
        self,
        access_token: str,
        arguments: dict[str, Any],
    ) -> None:
        envelope = await self._caller.call_tool(
            access_token=access_token,
            name="erpnext_get_doctype_schema",
            arguments={"doctype": arguments["doctype"]},
        )
        data = envelope.data
        fields = data.get("fields") if isinstance(data, dict) else None
        if not isinstance(fields, list):
            raise MCPContractError(
                "DocType schema returned invalid fields",
                code="INVALID_SCHEMA_RESULT",
            )
        writable_fields = {
            item.get("fieldname")
            for item in fields
            if isinstance(item, dict)
            and isinstance(item.get("fieldname"), str)
            and item.get("read_only") is False
        }
        unavailable = set(arguments["payload"]) - writable_fields
        if unavailable:
            raise ActionProposalError(
                f"Fields are not writable for the current user: {sorted(unavailable)}",
                code="FIELD_NOT_AVAILABLE",
            )

    async def _validate_current_document(
        self,
        access_token: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name != UPDATE_DRAFT_TOOL:
            return {}
        envelope = await self._caller.call_tool(
            access_token=access_token,
            name="erpnext_get_doc",
            arguments={"doctype": arguments["doctype"], "name": arguments["name"]},
        )
        data = envelope.data
        if not isinstance(data, dict):
            raise MCPContractError(
                "Draft read returned invalid data",
                code="INVALID_DRAFT_RESULT",
            )
        if data.get("docstatus") != 0:
            raise ActionProposalError("Only docstatus=0 drafts can be updated", code="NOT_A_DRAFT")
        current_modified = data.get("modified")
        if not isinstance(current_modified, str):
            raise MCPContractError(
                "Draft read returned no modified version",
                code="INVALID_DRAFT_RESULT",
            )
        if current_modified != arguments["expected_modified"]:
            raise ActionProposalError(
                "The draft changed after it was read; read it again and create a new preview",
                code="VERSION_CONFLICT",
            )
        return {"modified": current_modified}

    async def _validate_links(
        self,
        access_token: str,
        arguments: dict[str, Any],
    ) -> None:
        references = _collect_link_references(arguments["payload"])
        for doctype, names in references.items():
            ordered_names = sorted(names)
            found: set[str] = set()
            for start in range(0, len(ordered_names), 50):
                chunk = ordered_names[start : start + 50]
                envelope = await self._caller.call_tool(
                    access_token=access_token,
                    name="erpnext_get_list",
                    arguments={
                        "doctype": doctype,
                        "fields": ["name"],
                        "filters": {"name": ["in", chunk]},
                        "limit_start": 0,
                        "limit_page_length": 100,
                    },
                )
                data = envelope.data
                rows = data.get("rows") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise MCPContractError(
                        "Link validation returned invalid rows",
                        code="INVALID_LINK_RESULT",
                    )
                found.update(
                    name
                    for row in rows
                    if isinstance(row, dict)
                    if isinstance((name := row.get("name")), str)
                )
            missing = set(ordered_names) - found
            if missing:
                raise ActionProposalError(
                    f"{doctype} links were not found or are not visible: {sorted(missing)}",
                    code="LINK_NOT_FOUND",
                )


_ARGUMENTS_SCHEMA_DESCRIPTION = (
    "The draft operation arguments. Provide a nested object with the keys 'doctype' and "
    "'payload' (and, for updates only, 'name' and 'expected_modified'). 'payload' holds the "
    "draft fields. Required fields per DocType: Sales Order needs customer, company, "
    "transaction_date, delivery_date and items; Purchase Order needs supplier, company, "
    "transaction_date, schedule_date and items; Material Request needs "
    "material_request_type, company, transaction_date, schedule_date and items. Dates use "
    "YYYY-MM-DD. 'items' is an array of row objects; every row needs item_code and a "
    "positive numeric qty, and the target warehouse field is named 'warehouse'. Example: "
    "Warehouse values may be a bare label such as '仓库'; the service resolves a unique current-company "
    "warehouse alias, so do not require the user to provide the '- abbreviation' suffix. "
    "{\"doctype\": \"Sales Order\", \"payload\": {\"customer\": \"<name>\", \"company\": "
    "\"<name>\", \"transaction_date\": \"2026-08-17\", \"delivery_date\": \"2026-08-24\", "
    "\"items\": [{\"item_code\": \"<code>\", \"qty\": 1, \"warehouse\": \"<name>\"}]}}. "
    "Do not include idempotency_key; it is generated by the approval gateway."
)


class ActionProposalTool(ToolBase):
    """Persist a validated Action proposal without executing an ERPNext write."""

    name = PROPOSE_DRAFT_TOOL
    description = (
        "Validate a Sales Order, Purchase Order, or Material Request draft operation and "
        "create a persistent approval Action. This tool does not write to ERPNext."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "enum": [CREATE_DRAFT_TOOL, UPDATE_DRAFT_TOOL],
                "description": (
                    "Use erpnext_create_draft to create a new draft or "
                    "erpnext_update_draft to modify an existing draft."
                ),
            },
            "arguments": {"type": "object", "description": _ARGUMENTS_SCHEMA_DESCRIPTION},
        },
        "required": ["tool_name", "arguments"],
        "additionalProperties": False,
    }
    is_concurrency_safe = False
    is_read_only = False

    def __init__(
        self,
        *,
        service: ActionProposalService,
        session: AsyncSession,
        session_id: str,
        site: str,
        requested_by: str,
        access_token: str,
        conversation_id: str,
        telemetry: Telemetry | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._session = session
        self._session_id = session_id
        self._site = site
        self._requested_by = requested_by
        self._access_token = access_token
        self._conversation_id = conversation_id
        self._telemetry = telemetry or Telemetry.disabled()
        self.record: ActionRecord | None = None

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        del tool_input, context
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Creating a persistent preview is allowed; ERPNext is not modified.",
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        if self.record is not None:
            return _error_chunk(
                "ACTION_ALREADY_PROPOSED",
                "Only one Action proposal can be created in a single chat turn",
            )
        tool_name = kwargs.get("tool_name")
        arguments = kwargs.get("arguments")
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return _error_chunk("INVALID_ACTION_PROPOSAL", "Invalid proposal tool arguments")
        arguments = _normalize_model_arguments(arguments)
        doctype = arguments.get("doctype")
        span_attributes = {
            "tool_name": tool_name,
            "doctype": doctype if isinstance(doctype, str) else "unknown",
        }
        with self._telemetry.span("action.propose", attributes=span_attributes) as span:
            try:
                self.record = await self._service.propose(
                    self._session,
                    session_id=self._session_id,
                    site=self._site,
                    requested_by=self._requested_by,
                    access_token=self._access_token,
                    tool_name=tool_name,
                    arguments=arguments,
                    conversation_id=self._conversation_id,
                )
            except ActionProposalError as exc:
                self._record_proposal_failure(span, exc.code, doctype, "local_validation")
                return _error_chunk(exc.code, str(exc))
            except MCPError as exc:
                self._record_proposal_failure(span, exc.code, doctype, "mcp_validation")
                return _error_chunk(exc.code, str(exc), trace_id=exc.trace_id)
            set_span_result(span, "OK")
        payload = {"ok": True, "action": action_public_payload(self.record)}
        return ToolChunk(
            content=[
                TextBlock(
                    text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            ],
            state=ToolResultState.SUCCESS,
            metadata={"action_id": self.record.action_id},
        )

    @staticmethod
    def _record_proposal_failure(
        span: Span,
        code: str,
        doctype: Any,
        stage: str,
    ) -> None:
        """Record a privacy-safe failure signal: code, doctype and stage only."""
        set_span_result(span, code, error=True)
        span.set_attribute("error_code", code)
        span.set_attribute("stage", stage)
        normalized_doctype = doctype if isinstance(doctype, str) else None
        if normalized_doctype is not None:
            span.set_attribute("doctype", normalized_doctype)
        logger.warning(
            "action_proposal_failed",
            extra={
                "error_code": code,
                "doctype": normalized_doctype,
                "stage": stage,
            },
        )


def _normalize_model_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Accept the common flat tool-call shape while retaining strict validation.

    Some OpenAI-compatible models place draft fields beside ``doctype`` instead of
    nesting them under ``payload``. Convert only that transport shape here; the
    regular ``validate_action_arguments`` whitelist remains the authority for every
    field and required value.
    """
    arguments = _unwrap_double_nested(arguments)
    if "payload" in arguments or "doctype" not in arguments:
        return _normalize_item_aliases(arguments)
    envelope = {
        key: arguments[key]
        for key in ("doctype", "name", "expected_modified")
        if key in arguments
    }
    envelope["payload"] = {
        key: value
        for key, value in arguments.items()
        if key not in {"doctype", "name", "expected_modified", "idempotency_key"}
    }
    return _normalize_item_aliases(envelope)


def _unwrap_double_nested(arguments: dict[str, Any]) -> dict[str, Any]:
    """Unwrap ``{"arguments": {...}}`` envelopes some models emit.

    A top-level ``arguments`` key is never a legitimate draft field, so when it is the
    only key present we treat its value as the intended arguments object.
    """
    inner = arguments.get("arguments")
    if isinstance(inner, dict) and set(arguments) == {"arguments"}:
        return inner
    return arguments


def _normalize_item_aliases(arguments: dict[str, Any]) -> dict[str, Any]:
    """Map common model wording to the canonical ERPNext item shape.

    Handles three transport-shape issues without inventing business values:
    a single item object supplied instead of a one-row list, the aliases
    ``delivery_warehouse``/``quantity``, and a numeric ``qty`` provided as a string.
    Anything still invalid afterwards is rejected by ``validate_action_arguments``.
    """
    payload = arguments.get("payload")
    if not isinstance(payload, dict):
        return arguments
    items = payload.get("items")
    changed = False
    if isinstance(items, dict):
        items = [items]
        changed = True
    if not isinstance(items, list):
        return arguments
    normalized_items: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            normalized_items.append(item)
            continue
        row = dict(item)
        if "delivery_warehouse" in row:
            row.setdefault("warehouse", row.pop("delivery_warehouse"))
            changed = True
        if "quantity" in row:
            row.setdefault("qty", row.pop("quantity"))
            changed = True
        qty = row.get("qty")
        if isinstance(qty, str):
            coerced = _coerce_number(qty)
            if coerced is not None:
                row["qty"] = coerced
                changed = True
        normalized_items.append(row)
    if not changed:
        return arguments
    normalized_arguments = dict(arguments)
    normalized_payload = dict(payload)
    normalized_payload["items"] = normalized_items
    normalized_arguments["payload"] = normalized_payload
    return normalized_arguments


def _coerce_number(value: str) -> int | float | None:
    """Parse a numeric string; return ``None`` when it is not a finite number."""
    text = value.strip()
    if not text:
        return None
    try:
        number: int | float = int(text)
    except ValueError:
        try:
            number = float(text)
        except ValueError:
            return None
    if isinstance(number, float) and not math.isfinite(number):
        return None
    return number


def _normalize_payload(payload: dict[str, Any], definition: DraftDefinition) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field, value in payload.items():
        if field in _DATE_FIELDS:
            normalized[field] = _iso_date(value, field)
        elif field == "items":
            normalized[field] = _normalize_items(value, definition)
        else:
            _validate_json_value(value, path=field)
            if isinstance(value, str) and not value.strip():
                raise ActionProposalError(f"{field} must not be empty")
            normalized[field] = value.strip() if isinstance(value, str) else value
    return normalized


def _normalize_items(value: Any, definition: DraftDefinition) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ActionProposalError("items must contain at least one row")
    if len(value) > 100:
        raise ActionProposalError("items exceeds the 100-row limit")
    rows: list[dict[str, Any]] = []
    for index, raw_row in enumerate(value):
        if not isinstance(raw_row, dict) or not raw_row:
            raise ActionProposalError(f"items[{index}] must be a non-empty object")
        unexpected = set(raw_row) - definition.allowed_item_fields
        if unexpected:
            raise ActionProposalError(
                f"items[{index}] fields are not allowed: {sorted(unexpected)}"
            )
        item_code = _required_string(
            raw_row.get("item_code"),
            f"items[{index}].item_code",
            max_length=180,
        )
        qty = raw_row.get("qty")
        if (
            isinstance(qty, bool)
            or not isinstance(qty, (int, float))
            or not math.isfinite(qty)
            or qty <= 0
        ):
            raise ActionProposalError(f"items[{index}].qty must be a positive number")
        row: dict[str, Any] = {"item_code": item_code, "qty": qty}
        for field, item_value in raw_row.items():
            if field in row:
                continue
            if field in _DATE_FIELDS:
                row[field] = _iso_date(item_value, f"items[{index}].{field}")
            else:
                _validate_json_value(item_value, path=f"items[{index}].{field}")
                if isinstance(item_value, str) and not item_value.strip():
                    raise ActionProposalError(f"items[{index}].{field} must not be empty")
                row[field] = item_value.strip() if isinstance(item_value, str) else item_value
        rows.append(row)
    return rows


def _collect_link_references(payload: dict[str, Any]) -> dict[str, set[str]]:
    references: dict[str, set[str]] = {}

    def add(field: str, value: Any) -> None:
        doctype = _LINK_FIELDS.get(field)
        if doctype and isinstance(value, str) and value:
            references.setdefault(doctype, set()).add(value)

    for field, value in payload.items():
        if field == "items" and isinstance(value, list):
            for row in value:
                if isinstance(row, dict):
                    for item_field, item_value in row.items():
                        add(item_field, item_value)
        else:
            add(field, value)
    return references


def _required_string(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionProposalError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ActionProposalError(f"{field} exceeds the {max_length}-character limit")
    return normalized


def _iso_date(value: Any, field: str) -> str:
    normalized = _required_string(value, field, max_length=10)
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ActionProposalError(f"{field} must use YYYY-MM-DD") from exc


def _validate_json_value(value: Any, *, path: str, depth: int = 0) -> None:
    if depth > 6:
        raise ActionProposalError(f"{path} exceeds the nesting limit")
    if isinstance(value, str):
        if len(value) > 5_000:
            raise ActionProposalError(f"{path} exceeds the 5000-character limit")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ActionProposalError(f"{path} must be a finite number")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ActionProposalError(f"{path} contains a non-string key")
            _validate_json_value(item, path=f"{path}.{key}", depth=depth + 1)
        return
    raise ActionProposalError(f"{path} contains an unsupported value")


def _error_chunk(code: str, message: str, *, trace_id: str | None = None) -> ToolChunk:
    payload: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    metadata: dict[str, Any] = {}
    if trace_id:
        payload["meta"] = {"trace_id": trace_id}
        metadata["trace_id"] = trace_id
    return ToolChunk(
        content=[TextBlock(text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))],
        state=ToolResultState.ERROR,
        metadata=metadata,
    )
