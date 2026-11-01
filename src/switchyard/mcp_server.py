"""MCP server: read-only access to the business apps.

Agents see the CRM, calendar and document templates through MCP. There are no
write tools here on purpose: writes go through the Switchyard outbox, which is
where policy, approval, idempotency and verification live. MCP gives agents
eyes, and the engine keeps the hands.

Run standalone (e.g. for ChatGPT / Claude desktop or the MCP inspector):

    python -m switchyard.mcp_server            # stdio
    python -m switchyard.mcp_server --http 8765
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from .sandbox import SURVEY_CALENDAR, SandboxApps

server = MCPServer(
    name="switchyard-business-apps",
    instructions=(
        "Read-only views of the company CRM, survey calendar and proposal templates. "
        "This server cannot change anything; actions are proposed to the Switchyard engine instead."
    ),
)

_apps: SandboxApps | None = None


def apps() -> SandboxApps:
    global _apps
    if _apps is None:
        path = os.environ.get("SWITCHYARD_SANDBOX_DB")
        if not path:
            raise RuntimeError("SWITCHYARD_SANDBOX_DB is not set")
        _apps = SandboxApps(path)
    return _apps


def _public_contact(c: dict[str, Any]) -> dict[str, Any]:
    return {k: c.get(k) for k in ("id", "email", "name", "company", "phone", "segment")}


@server.tool()
def crm_search_contacts(query: str) -> list[dict[str, Any]]:
    """Find CRM contacts whose email, name or company contains `query` (case-insensitive)."""
    return [_public_contact(c) for c in apps().crm_search_contacts(query)]


@server.tool()
def crm_list_deals(email: str) -> list[dict[str, Any]]:
    """List deals for the contact with this email address, oldest first."""
    return [
        {k: d[k] for k in ("id", "title", "stage", "amount", "currency", "owner")}
        for d in apps().crm_deals_for_email(email)
    ]


@server.tool()
def calendar_free_slots(start_date: str, days: int = 7, duration_minutes: int = 90) -> list[dict[str, str]]:
    """Free site-survey slots on the surveyor calendar, starting on `start_date` (YYYY-MM-DD)."""
    return apps().calendar_free_slots(SURVEY_CALENDAR, start_date, days, duration_minutes)


@server.tool()
def docs_get_template(name: str) -> dict[str, Any]:
    """Return a proposal template: its sections, required disclaimer and tone rules."""
    template = apps().docs_template(name)
    if template is None:
        raise ValueError(f"no template named {name!r}")
    return template


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", type=int, help="serve streamable HTTP on this port instead of stdio")
    args = parser.parse_args()
    if args.http:
        server.run("streamable-http", host="127.0.0.1", port=args.http)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
