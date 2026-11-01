"""Turn engine records into what a person reads: labels, tones, subjects, relative times."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

from markupsafe import Markup, escape

WORKFLOWS = {
    "inbound_triage": "Inbound triage",
    "lead_to_proposal": "Lead to proposal",
    "service_request": "Service request",
    "survey_to_install": "Survey to install",
}

STEP_TITLES = {
    "route": "Read and route the message",
    "check_existing": "Check for an open deal",
    "crm_record": "Record contact and deal",
    "estimate": "Size the system",
    "draft_proposal": "Draft the proposal",
    "review": "Review the draft",
    "schedule_survey": "Hold a survey slot",
    "proposal_document": "File the proposal",
    "send_proposal": "Email the customer",
    "crm_stage": "Update the deal",
    "notify_team": "Tell the sales team",
    "log_case": "Log the case",
    "alert_team": "Alert the service team",
    "acknowledge": "Reply to the customer",
    "update_deal": "Update the deal",
    "handover_pack": "Prepare the handover pack",
    "deposit_invoice": "Issue the deposit invoice",
    "notify_ops": "Tell operations",
}

ACTIONS = {
    "email.send": "Send email",
    "crm.upsert_contact": "Save CRM contact",
    "crm.create_deal": "Open CRM deal",
    "crm.update_deal_stage": "Move deal stage",
    "crm.add_note": "Add CRM note",
    "docs.create": "Create document",
    "calendar.create_event": "Hold calendar slot",
    "chat.post": "Post to team chat",
    "billing.issue_invoice": "Issue invoice",
}

RISK = {
    "internal_routine": ("Internal routine", "neutral", "Runs on its own. Stays inside the company."),
    "external_communication": ("External communication", "info", "Goes to someone outside the company."),
    "binding_commitment": ("Binding commitment", "warn", "Commits the company to a price or a payment."),
    "sensitive_disclosure": ("Sensitive disclosure", "bad", "Contains information that must not leave unreviewed."),
}

KINDS = {
    "tool_permanent": "Integration refused",
    "tool_retries_exhausted": "Integration unavailable",
    "verification_failed": "Write not found at destination",
    "review_rejected": "Draft failed review",
    "budget_exceeded": "Model budget reached",
    "approval_rejected": "Rejected by a person",
    "policy_violation": "Agent overstepped its permissions",
    "needs_decision": "Needs a decision",
    "lease_recovered": "Worker interrupted, resumed",
    "agent_error": "Model error",
    "insufficient_data": "Not enough information",
    "integrity_error": "Content changed after review",
    "step_error": "Step error",
    "step_failed": "Step failed",
}

RETRYABLE_KINDS = {
    "tool_permanent", "tool_retries_exhausted", "verification_failed", "review_rejected",
    "budget_exceeded", "agent_error", "step_error", "step_failed", "insufficient_data",
}  # fmt: skip

TONES = {
    "succeeded": "ok", "verified": "ok", "approved": "ok", "decided": "ok", "resolved": "ok",
    "running": "info", "executing": "info", "executed": "info",
    "waiting": "warn", "awaiting_approval": "warn", "retry_wait": "warn", "retry": "warn", "open": "warn",
    "failed": "bad", "blocked": "bad", "rejected": "bad", "expired": "bad", "critical": "bad", "high": "bad",
    "pending": "neutral", "proposed": "neutral", "superseded": "neutral", "low": "neutral", "info": "neutral",
}  # fmt: skip

STATUS_LABELS = {
    "retry_wait": "Retrying",
    "awaiting_approval": "Awaiting approval",
    "succeeded": "Done",
    "running": "Running",
    "waiting": "Waiting",
    "pending": "Queued",
    "failed": "Failed",
    "blocked": "Blocked",
    "verified": "Verified",
}


def label_workflow(name: str) -> str:
    return WORKFLOWS.get(name, name.replace("_", " ").capitalize())


def label_step(name: str) -> str:
    return STEP_TITLES.get(name, name.replace("_", " ").capitalize())


def label_action(action: str) -> str:
    return ACTIONS.get(action, action)


def label_kind(kind: str) -> str:
    return KINDS.get(kind, kind.replace("_", " ").capitalize())


def label_status(status: str) -> str:
    return STATUS_LABELS.get(status, status.replace("_", " ").capitalize())


def tone(status: str) -> str:
    return TONES.get(status, "neutral")


def risk(cls: str) -> tuple[str, str, str]:
    return RISK.get(cls, (cls, "neutral", ""))


def run_subject(run: dict[str, Any] | None) -> str:
    """The thing a run is about, in the words a salesperson would use."""
    if not run:
        return ""
    data = run.get("input", {})
    lead = data.get("lead")
    if lead:
        return lead.get("company") or lead.get("contact_name") or lead.get("email", "")
    case = data.get("case")
    if case:
        return case.get("customer_name") or case.get("customer_email", "")
    message = data.get("message")
    if message:
        return message.get("from_name") or message.get("from_email", "")
    if "deal_id" in data:
        return data.get("customer_email") or data["deal_id"]
    return run["id"]


def run_context(run: dict[str, Any] | None) -> str:
    if not run:
        return ""
    data = run.get("input", {})
    if data.get("message"):
        return data["message"].get("subject", "")
    if data.get("survey"):
        return f"Survey by {data['survey'].get('surveyor', 'surveyor')}"
    return ""


def relative(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return ""
    delta = (now or time.time()) - ts
    future = delta < 0
    delta = abs(delta)
    if delta < 45:
        text = "just now" if not future else "in under a minute"
        return text
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if delta >= size:
            n = int(delta // size)
            return f"in {n} {unit}" if future else f"{n} {unit} ago"
    return ""


def absolute(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%d %b %Y, %H:%M UTC") if ts else ""


def duration(start: float | None, end: float | None) -> str:
    if not start or not end:
        return ""
    s = max(0.0, end - start)
    if s < 1:
        return f"{s * 1000:.0f} ms"
    if s < 90:
        return f"{s:.1f} s"
    return f"{s / 60:.0f} min"


def money(value: float | None, currency: str | None = None) -> str:
    if value is None:
        return ""
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency or "", "")
    return f"{symbol}{value:,.0f}" if symbol else f"{value:,.0f} {currency or ''}".strip()


def usd(value: float | None) -> str:
    value = value or 0.0
    return f"${value:,.4f}" if value < 1 else f"${value:,.2f}"


def short(identifier: str | None, keep: int = 6) -> str:
    if not identifier:
        return ""
    prefix, _, rest = identifier.partition("_")
    return f"{prefix}_…{rest[-keep:]}" if rest and len(rest) > keep + 2 else identifier


def initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s.\-]+", name) if p and p[0].isalpha()]
    return "".join(p[0].upper() for p in parts[:2]) or "?"


_URL = re.compile(r"https?://[^\s<>\"')]+")


def linkify(text: str) -> Markup:
    """Escape text, keep line breaks, and turn URLs into links."""
    out = []
    last = 0
    for m in _URL.finditer(text):
        out.append(escape(text[last : m.start()]))
        url = m.group(0)
        out.append(Markup('<a href="{0}" target="_blank" rel="noopener">{0}</a>').format(url))
        last = m.end()
    out.append(escape(text[last:]))
    return Markup("").join(out)


def first_name(name: str | None) -> str:
    return (name or "").split(" ")[0]


def greeting(now: float | None = None) -> str:
    hour = datetime.fromtimestamp(now or time.time()).hour
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"


FILTERS = {
    "workflow": label_workflow,
    "step": label_step,
    "action": label_action,
    "kind": label_kind,
    "status": label_status,
    "tone": tone,
    "rel": relative,
    "abs": absolute,
    "usd": usd,
    "short": short,
    "initials": initials,
    "linkify": linkify,
    "first_name": first_name,
}
