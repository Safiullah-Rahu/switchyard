from datetime import datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from switchyard import mcp_server
from switchyard.sandbox import SandboxApps

TOOLS = {"crm_search_contacts", "crm_list_deals", "calendar_free_slots", "docs_get_template"}


@pytest.fixture
def apps(tmp_path, monkeypatch):
    sandbox = SandboxApps(tmp_path / "sandbox.db")
    sandbox.seed(datetime(2026, 11, 2))  # a Monday; the seed books Tue 08:00, Wed 11:00 and Thu 14:00
    monkeypatch.setattr(mcp_server, "_apps", sandbox)
    yield sandbox
    sandbox.close()


async def _call(tool, /, **arguments):
    result = await mcp_server.server.call_tool(tool, arguments)
    assert not result.is_error
    # MCP wraps a return value that is not an object as {"result": ...}
    return result.structured_content.get("result", result.structured_content)


async def test_every_tool_declares_all_four_hints_as_read_only():
    tools = await mcp_server.server.list_tools()
    assert {t.name for t in tools} == TOOLS
    for tool in tools:
        hints = tool.annotations.model_dump(by_alias=True, exclude_none=True)
        assert hints == {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


async def test_crm_search_contacts_returns_only_public_fields(apps):
    [contact] = await _call("crm_search_contacts", query="HARBOUR")
    assert contact["email"] == "j.rivera@harbourcold.example.com"
    assert set(contact) == {"id", "email", "name", "company", "phone", "segment"}
    assert await _call("crm_search_contacts", query="no such company") == []


async def test_crm_list_deals_for_a_contact(apps):
    [deal] = await _call("crm_list_deals", email="j.rivera@harbourcold.example.com")
    assert deal == {
        "id": "dl_seed_rivera",
        "title": "Harbour Cold Storage — rooftop PV",
        "stage": "qualified",
        "amount": 96000,
        "currency": "GBP",
        "owner": "maya.patel",
    }
    assert await _call("crm_list_deals", email="nobody@example.com") == []


async def test_calendar_free_slots_skip_bookings_and_weekends(apps):
    slots = await _call("calendar_free_slots", start_date="2026-11-02", days=7)
    starts = [s["start"] for s in slots]
    assert "2026-11-02T08:30" in starts
    assert "2026-11-03T08:30" not in starts  # inside Tuesday's 08:00–11:00 booking
    assert not any(s["weekday"] in ("Saturday", "Sunday") for s in slots)
    assert len(slots) == 5 * 3 - 3


async def test_docs_get_template_and_an_unknown_name(apps):
    template = await _call("docs_get_template", name="commercial-solar-proposal")
    assert template["name"] == "commercial-solar-proposal" and template["sections"]
    with pytest.raises(ToolError):
        await mcp_server.server.call_tool("docs_get_template", {"name": "no-such-template"})


async def test_tools_write_nothing(apps):
    before = apps.conn.total_changes
    await _call("crm_search_contacts", query="harbour")
    await _call("crm_list_deals", email="j.rivera@harbourcold.example.com")
    await _call("calendar_free_slots", start_date="2026-11-02")
    await _call("docs_get_template", name="commercial-solar-proposal")
    assert apps.conn.total_changes == before
