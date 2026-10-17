"""Tests for the three MCP tools - executed through a real (in-memory) MCP client.

Run:  uv run pytest -q      (or: python -m pytest -q)
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit_log  # noqa: E402
import inventory_db  # noqa: E402
from discount_engine import compute_tiered_discount  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory_db, "DB_PATH", tmp_path / "inventory.db")
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", tmp_path / "audit.log")


def call(tool: str, args: dict):
    from fastmcp import Client
    import server

    async def _run():
        async with Client(server.mcp) as c:
            res = await c.call_tool(tool, args, raise_on_error=False)
            text = res.content[0].text
            is_error = getattr(res, "is_error", None)
            if is_error is None:
                is_error = getattr(res, "isError", False)
            return is_error, text

    return asyncio.run(_run())


# ---------------- discount engine (pure formula) ----------------
def test_no_discount_below_10_units():
    r = compute_tiered_discount(9, 100.0)
    assert r["final_discount_rate_pct"] == 0 and r["net_total"] == 900.0


def test_graduated_tiers_are_marginal():
    # 60 units: 9 @0%, 40 @5%, 11 @10%  -> discount = 40*5 + 11*10 = 310 on 6000 gross
    r = compute_tiered_discount(60, 100.0)
    assert r["discount_amount"] == pytest.approx(310.0)
    assert r["blended_volume_rate_pct"] == pytest.approx(310 / 6000 * 100, abs=1e-3)


def test_loyalty_and_value_bonus_and_cap():
    r = compute_tiered_discount(500, 178.0, "platinum", "Safety")
    assert r["order_value_bonus_pct"] == 3.0          # gross 89 000 >= 50 000
    assert r["cap_applied"] is True
    assert r["final_discount_rate_pct"] == 15.0        # Safety cap
    assert r["net_total"] == pytest.approx(75650.0)


def test_invalid_input_rejected():
    with pytest.raises(ValueError):
        compute_tiered_discount(0, 10.0)
    with pytest.raises(ValueError):
        compute_tiered_discount(5, 10.0, "diamond")


# ---------------- MCP tools end-to-end ----------------
def test_query_inventory_by_sku():
    err, text = call("query_inventory", {"sku": "plc-1200"})
    data = json.loads(text)
    assert not err and data["count"] == 1 and data["products"][0]["unit_price"] == 389.0


def test_query_inventory_low_stock():
    err, text = call("query_inventory", {"low_stock_only": True})
    skus = {p["sku"] for p in json.loads(text)["products"]}
    assert {"PLC-1500", "HMI-1200", "DRV-0550"} <= skus


def test_query_inventory_sql_injection_is_harmless():
    err, text = call("query_inventory", {"search": "x' OR '1'='1", "sort_by": "sku; DROP TABLE products"})
    assert json.loads(text)["count"] == 0
    err, text = call("query_inventory", {})
    assert json.loads(text)["count"] == 14  # table still intact


def test_discount_tool_uses_db_price_and_checks_stock():
    err, text = call("calculate_tiered_discount", {"quantity": 500, "sku": "SAF-RLY01", "customer_tier": "platinum"})
    data = json.loads(text)
    assert data["unit_price"] == 178.0 and data["stock_check"]["shortfall"] == 360


def test_discount_tool_unknown_sku_is_tool_error():
    err, text = call("calculate_tiered_discount", {"quantity": 5, "sku": "NOPE-1"})
    assert err and "Unknown SKU" in text


def test_audit_log_appends_and_sanitizes():
    call("append_audit_log", {"event_type": "quote created", "message": "line1\nline2", "details": {"sku": "X"}})
    err, text = call("append_audit_log", {"event_type": "LOW_STOCK_ALERT", "message": "m", "level": "warning"})
    entry = json.loads(text)["entry"]
    assert entry["seq"] == 2 and entry["level"] == "WARNING"
    lines = audit_log.AUDIT_LOG_PATH.read_text().splitlines()
    first = json.loads(lines[0])
    assert first["event_type"] == "QUOTE_CREATED" and first["message"] == "line1 line2"


def test_audit_log_rejects_bad_level():
    err, text = call("append_audit_log", {"event_type": "X", "message": "m", "level": "DEBUG"})
    assert err
