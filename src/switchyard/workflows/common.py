"""Helpers shared by workflow definitions."""

from __future__ import annotations

import re
from typing import Any

from ..canonical import payload_hash
from ..estimator import Estimate


def render_inbound(message: dict[str, Any]) -> str:
    return (
        "<inbound>\n"
        f"From: {message['from_name']} <{message['from_email']}>\n"
        f"Subject: {message['subject']}\n"
        f"Received: {message.get('received_at', '')}\n\n"
        f"{message['body'].strip()}\n"
        "</inbound>"
    )


def inbound_dedup(input: dict[str, Any]) -> str:
    """The same message re-delivered (new event id, same content) is the same business event."""
    m = input["message"]
    return (
        "inbound:"
        + payload_hash({"from": m["from_email"].lower(), "subject": m["subject"].strip(), "body": m["body"].strip()})[
            :24
        ]
    )


def lead_dedup(lead: dict[str, Any]) -> str:
    """One proposal workflow per person per site. A second site for the same customer is a new lead."""
    site = lead.get("site_address") or lead.get("company") or ""
    site = re.sub(r"[^a-z0-9]", "", site.lower())
    return f"lead:{lead['email'].lower()}:{site}"


def estimate_for_agent(est: Estimate) -> dict[str, Any]:
    """What get_estimate() returns: values, units and the exact display strings to use."""
    s = est.symbol
    display = {
        "system_kwp": f"{est.system_kwp} kWp",
        "annual_generation_kwh": f"{est.annual_generation_kwh:,} kWh",
        "annual_savings": f"{s}{est.annual_savings:,}",
        "capex": f"{s}{est.capex:,}",
        "payback_years": f"{est.payback_years} years",
        "lifetime_savings": f"{s}{est.lifetime_savings:,}",
        "co2_tonnes_per_year": f"{est.co2_tonnes_per_year} tonnes a year",
        "self_consumption_pct": f"{est.self_consumption_pct}%",
    }
    if est.battery_kwh:
        display["battery_kwh"] = f"{est.battery_kwh} kWh"
    data = est.model_dump()
    data["figures"] = {k: {"value": v, "unit": u} for k, (v, u) in est.figures().items()}
    data["display"] = display
    return data
