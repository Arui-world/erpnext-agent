from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from erpnext_agent.mcp.adapter import MCPContractError, MCPEnvelope

BIN_PAGE_LENGTH = 100
BIN_MAX_PAGES = 10


class InventoryIdentityError(PermissionError):
    pass


class InventoryMCPCaller(Protocol):
    async def current_user(
        self,
        access_token: str,
        *,
        discover_first: bool = True,
    ) -> str: ...

    async def call_tool(
        self,
        *,
        access_token: str,
        name: str,
        arguments: dict[str, Any],
        discover_first: bool = False,
    ) -> MCPEnvelope: ...


@dataclass(frozen=True, slots=True)
class ItemMatch:
    item_code: str
    item_name: str | None
    stock_uom: str | None


@dataclass(frozen=True, slots=True)
class WarehouseStock:
    warehouse: str
    actual_qty: Decimal
    reserved_qty: Decimal
    projected_qty: Decimal


@dataclass(frozen=True, slots=True)
class MultiWarehouseStockResult:
    requested_item: str
    item: ItemMatch | None
    candidates: tuple[ItemMatch, ...]
    warehouses: tuple[WarehouseStock, ...]
    truncated: bool
    tool_calls: tuple[str, ...]


class InventoryService:
    """Permission-aware deterministic queries for common stock questions."""

    def __init__(self, adapter: InventoryMCPCaller) -> None:
        self._adapter = adapter

    async def positive_stock_by_warehouse(
        self,
        *,
        access_token: str,
        expected_user: str,
        requested_item: str,
    ) -> MultiWarehouseStockResult:
        current_user = await self._adapter.current_user(access_token, discover_first=True)
        if not secrets.compare_digest(current_user.casefold(), expected_user.casefold()):
            raise InventoryIdentityError(
                "Agent session and ERPNext MCP identities do not match"
            )

        tool_calls: list[str] = []
        candidates = await self._find_items(
            access_token=access_token,
            requested_item=requested_item,
            tool_calls=tool_calls,
        )
        if len(candidates) != 1:
            return MultiWarehouseStockResult(
                requested_item=requested_item,
                item=None,
                candidates=tuple(candidates),
                warehouses=(),
                truncated=False,
                tool_calls=tuple(tool_calls),
            )

        item = candidates[0]
        warehouses: list[WarehouseStock] = []
        truncated = False
        for page in range(BIN_MAX_PAGES):
            envelope = await self._adapter.call_tool(
                access_token=access_token,
                name="erpnext_get_list",
                arguments={
                    "doctype": "Bin",
                    "fields": [
                        "item_code",
                        "warehouse",
                        "actual_qty",
                        "reserved_qty",
                        "projected_qty",
                    ],
                    "filters": {
                        "item_code": item.item_code,
                        "actual_qty": [">", 0],
                    },
                    "order_by": "actual_qty desc",
                    "limit_start": page * BIN_PAGE_LENGTH,
                    "limit_page_length": BIN_PAGE_LENGTH,
                },
            )
            tool_calls.append("erpnext_get_list")
            rows = _list_rows(envelope, expected_doctype="Bin")
            warehouses.extend(_warehouse_stock(row) for row in rows)
            if len(rows) < BIN_PAGE_LENGTH:
                break
        else:
            truncated = True

        return MultiWarehouseStockResult(
            requested_item=requested_item,
            item=item,
            candidates=tuple(candidates),
            warehouses=tuple(row for row in warehouses if row.actual_qty > 0),
            truncated=truncated,
            tool_calls=tuple(tool_calls),
        )

    async def _find_items(
        self,
        *,
        access_token: str,
        requested_item: str,
        tool_calls: list[str],
    ) -> list[ItemMatch]:
        exact = await self._get_items(
            access_token=access_token,
            filters={"name": requested_item},
        )
        tool_calls.append("erpnext_get_list")
        if exact:
            return exact

        by_name = await self._get_items(
            access_token=access_token,
            filters={"item_name": requested_item},
        )
        tool_calls.append("erpnext_get_list")
        return by_name

    async def _get_items(
        self,
        *,
        access_token: str,
        filters: dict[str, Any],
    ) -> list[ItemMatch]:
        envelope = await self._adapter.call_tool(
            access_token=access_token,
            name="erpnext_get_list",
            arguments={
                "doctype": "Item",
                "fields": ["name", "item_name", "stock_uom"],
                "filters": filters,
                "order_by": "name asc",
                "limit_page_length": 20,
            },
        )
        return [_item_match(row) for row in _list_rows(envelope, expected_doctype="Item")]


_STOCK_QUERY = re.compile(
    r"^\s*(?:(?:请|麻烦)?(?:帮我)?(?:查询|查看|查一下|查)?\s*)?"
    r"(?:物料(?:编码|代码)?\s*)?[“\"']?(?P<item>.+?)[”\"']?\s*"
    r"(?:的)?库存(?:情况|数量|余额)?\s*[？?]?\s*$",
    flags=re.IGNORECASE,
)
_GENERIC_ITEMS = frozenset({"当前", "所有", "全部", "物料", "商品", "产品"})


def extract_multiwarehouse_stock_item(message: str) -> str | None:
    """Extract an item from common no-warehouse stock questions."""

    match = _STOCK_QUERY.fullmatch(message)
    if match is None:
        return None
    item = match.group("item").strip().strip("“”\"'")
    item = re.sub(r"\s*在(?:所有|全部|各个?|每个)?仓库(?:中)?$", "", item).strip()
    if not item or item in _GENERIC_ITEMS or "在" in item or len(item) > 180:
        return None
    return item


def format_multiwarehouse_stock(result: MultiWarehouseStockResult) -> str:
    requested = _markdown_text(result.requested_item)
    if result.item is None:
        if not result.candidates:
            return f"未找到物料 **{requested}**，请检查物料编码或名称。"
        candidate_codes = "、".join(
            f"`{_markdown_text(item.item_code)}`" for item in result.candidates
        )
        return f"找到多个名为 **{requested}** 的物料，请选择物料编码：{candidate_codes}。"

    item_code = _markdown_text(result.item.item_code)
    item_name = _markdown_text(result.item.item_name or "")
    label = item_code if not item_name or item_name == item_code else f"{item_code}（{item_name}）"
    unit = _markdown_text(result.item.stock_uom or "")
    if not result.warehouses:
        return f"物料 **{label}** 当前没有实际库存大于 0 的仓库。"

    lines = [
        f"物料 **{label}** 在以下仓库有正库存：",
        "",
        "| 仓库 | 实际库存 | 预留数量 | 预计数量 |",
        "|---|---:|---:|---:|",
    ]
    for row in result.warehouses:
        suffix = f" {unit}" if unit else ""
        lines.append(
            f"| {_markdown_cell(row.warehouse)} | {_number(row.actual_qty)}{suffix} | "
            f"{_number(row.reserved_qty)}{suffix} | {_number(row.projected_qty)}{suffix} |"
        )
    total = sum((row.actual_qty for row in result.warehouses), start=Decimal(0))
    suffix = f" {unit}" if unit else ""
    lines.extend(["", f"合计实际库存：**{_number(total)}{suffix}**。"])
    if result.truncated:
        lines.append("结果已达 1000 条安全上限，可能仍有更多仓库未显示。")
    return "\n".join(lines)


def _list_rows(envelope: MCPEnvelope, *, expected_doctype: str) -> list[dict[str, Any]]:
    data = envelope.data
    if not isinstance(data, dict) or data.get("doctype") != expected_doctype:
        raise MCPContractError(
            f"Expected {expected_doctype} list response",
            code="INVALID_LIST_RESPONSE",
        )
    rows = data.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise MCPContractError("List response has invalid rows", code="INVALID_LIST_RESPONSE")
    return rows


def _item_match(row: dict[str, Any]) -> ItemMatch:
    item_code = row.get("name")
    if not isinstance(item_code, str) or not item_code:
        raise MCPContractError("Item row has no name", code="INVALID_ITEM_ROW")
    item_name = row.get("item_name")
    stock_uom = row.get("stock_uom")
    return ItemMatch(
        item_code=item_code,
        item_name=item_name if isinstance(item_name, str) else None,
        stock_uom=stock_uom if isinstance(stock_uom, str) else None,
    )


def _warehouse_stock(row: dict[str, Any]) -> WarehouseStock:
    warehouse = row.get("warehouse")
    if not isinstance(warehouse, str) or not warehouse:
        raise MCPContractError("Bin row has no warehouse", code="INVALID_BIN_ROW")
    return WarehouseStock(
        warehouse=warehouse,
        actual_qty=_decimal(row.get("actual_qty"), field="actual_qty"),
        reserved_qty=_decimal(row.get("reserved_qty"), field="reserved_qty"),
        projected_qty=_decimal(row.get("projected_qty"), field="projected_qty"),
    )


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError) as exc:
        raise MCPContractError(
            f"Bin row has invalid {field}",
            code="INVALID_BIN_ROW",
        ) from exc


def _number(value: Decimal) -> str:
    return format(value, "f")


def _markdown_text(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+!|])", r"\\\1", value.replace("\n", " "))


def _markdown_cell(value: str) -> str:
    return _markdown_text(value)
