from __future__ import annotations

from typing import Any

import pytest

from erpnext_agent.inventory import (
    InventoryIdentityError,
    InventoryService,
    extract_multiwarehouse_stock_item,
    format_multiwarehouse_stock,
)
from erpnext_agent.mcp.adapter import MCPEnvelope


class FakeInventoryAdapter:
    def __init__(self, *, user: str = "Administrator", bins: list[dict[str, Any]] | None = None):
        self.user = user
        self.bins = bins or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def current_user(
        self,
        _access_token: str,
        *,
        discover_first: bool = True,
    ) -> str:
        assert discover_first is True
        return self.user

    async def call_tool(
        self,
        *,
        access_token: str,
        name: str,
        arguments: dict[str, Any],
        discover_first: bool = False,
    ) -> MCPEnvelope:
        assert access_token == "secret-token"  # noqa: S105 - inert test value
        assert discover_first is False
        self.calls.append((name, arguments))
        if arguments["doctype"] == "Item":
            rows = (
                [{"name": "test item1", "item_name": "test item1", "stock_uom": "Nos"}]
                if arguments["filters"] == {"name": "test item1"}
                else []
            )
            return MCPEnvelope(data={"doctype": "Item", "rows": rows}, meta={})
        return MCPEnvelope(data={"doctype": "Bin", "rows": self.bins}, meta={})


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("test item1的库存", "test item1"),
        ("查询物料 test item1 库存", "test item1"),
        ("test item1在所有仓库的库存", "test item1"),
        ("查看当前库存", None),
        ("test item1在 Stores - TQC 的库存", None),
    ],
)
def test_extract_multiwarehouse_stock_item(message: str, expected: str | None) -> None:
    assert extract_multiwarehouse_stock_item(message) == expected


async def test_positive_stock_query_uses_item_and_filtered_bin_lists() -> None:
    adapter = FakeInventoryAdapter(
        bins=[
            {
                "warehouse": "仓库 - rw",
                "actual_qty": 10.0,
                "reserved_qty": 0.0,
                "projected_qty": 10.0,
            }
        ]
    )
    result = await InventoryService(adapter).positive_stock_by_warehouse(
        access_token="secret-token",  # noqa: S106 - inert test value
        expected_user="Administrator",
        requested_item="test item1",
    )

    assert result.item is not None
    assert result.item.item_code == "test item1"
    assert result.warehouses[0].warehouse == "仓库 - rw"
    assert adapter.calls[1][1]["filters"] == {
        "item_code": "test item1",
        "actual_qty": [">", 0],
    }
    reply = format_multiwarehouse_stock(result)
    assert "仓库 - rw" in reply
    assert "10.0 Nos" in reply
    assert "合计实际库存" in reply


async def test_inventory_identity_mismatch_fails_before_business_query() -> None:
    adapter = FakeInventoryAdapter(user="other@example.com")
    with pytest.raises(InventoryIdentityError):
        await InventoryService(adapter).positive_stock_by_warehouse(
            access_token="secret-token",  # noqa: S106 - inert test value
            expected_user="Administrator",
            requested_item="test item1",
        )
    assert adapter.calls == []
