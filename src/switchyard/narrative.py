"""Plain-language descriptions of audit events, shared by the console and the demo reports."""

from __future__ import annotations

from typing import Any


def describe(e: dict[str, Any], intents: dict[str, dict[str, Any]] | None = None) -> str | None:
    d, s, ev = e["data"], e["subject"], e["event"]
    intent = (intents or {}).get(s or "")
    what = f"{intent['action']} [{intent['slot']}]" if intent else s
    if ev == "run.started":
        return f"run {e['run_id']} started: {d['workflow']}" + (
            f" (child of {d['parent_run_id']})" if d.get("parent_run_id") else ""
        )
    if ev == "run.duplicate_suppressed":
        return f"duplicate business event suppressed → existing run {e['run_id']}"
    if ev == "webhook.duplicate":
        return f"webhook redelivery of event {s} ignored"
    if ev == "webhook.rejected":
        return "webhook with a bad signature rejected (401)"
    if ev == "agent.completed":
        via = f" via handoff from {d['started_with']}" if d.get("handoffs") else ""
        tools = ", ".join(d["tool_calls"]) or "no tools"
        return f"{d['finished_with']}{via} finished [{s}] using {tools}; {d['proposals']} proposal(s), {len(d['refused_proposals'])} refused"
    if ev == "agent.proposal_refused":
        return f"REFUSED {d['action']} proposed by {d['agent']}: {d['reason']}"
    if ev == "intent.proposed" and (d.get("requires_approval") or d.get("risk_class") != "internal_routine"):
        return f"{d['action']} classified {d['risk_class']} — {'; '.join(d['reasons'][1:]) or d['reasons'][0]}"
    if ev == "intent.verified":
        return f"{what}: read back and verified at the destination"
    if ev == "intent.executed":
        return f"{what}: written → {d['receipt']['external_id']}" + (
            " (adopted existing)" if d["receipt"]["details"].get("adopted_on_conflict") else ""
        )
    if ev == "intent.transient_error":
        return f"transient failure: {d['error']} — retry in {d['retry_in_s']}s"
    if ev == "intent.outcome_unknown":
        return f"outcome unknown: {d['error']}"
    if ev == "intent.reconciled":
        return f"reconciled with destination before acting again: {'already there → adopted, not resent' if d['found_at_destination'] else 'not there → safe to send'}"
    if ev == "intent.verification_failed":
        return f"VERIFICATION FAILED: {'; '.join(d['mismatches'])}"
    if ev == "intent.revised":
        return f"{what}: content changed ({d['old_hash'][:8]} → {d['new_hash'][:8]})"
    if ev in ("intent.rejected", "intent.refused"):
        return f"{s} not executed: {d['reason']}"
    if ev == "approval.requested":
        return f"approval {s} requested for {d['action']} ({d['risk_class']}) from {', '.join(d['required_roles'])}"
    if ev in ("approval.approved", "approval.rejected"):
        return f"approval {s} {ev.split('.')[1]} by {e['actor']}"
    if ev == "approval.denied_unauthorised":
        return f"{e['actor']} tried to approve {s} without a required role — denied"
    if ev == "approval.not_usable":
        return f"approval {s} not usable ({d['status']}): {d['reason']}"
    if ev == "step.waiting":
        return f"step {s} waiting on {d['on']}"
    if ev == "step.woken":
        return f"step {s} resumed by {e['actor']}"
    if ev == "step.retry_scheduled":
        return f"step {s} attempt {d['attempt']} failed ({d['error'][:90]}) — retry in {d['retry_in_s']}s"
    if ev == "step.failed":
        return f"step {s} FAILED [{d['kind']}]: {d['error'][:120]}" + (
            f" — blocked: {', '.join(d['blocked'])}" if d["blocked"] else ""
        )
    if ev == "step.rewound":
        return f"step {s} sent back by {d['by']} (revision {d['revision']}): {d['note'][:140]}"
    if ev == "step.lease_expired":
        return f"lease on {s} held by {d['previous_owner']} expired — step resumed by a new worker"
    if ev == "step.retry_requested":
        return f"operator {e['actor']} retried {s}"
    if ev == "decision.requested":
        return f"decision needed: {d['question'][:140]}"
    if ev == "decision.made":
        return f"decision made by {e['actor']}: {d['choice']}"
    if ev == "exception.raised":
        return f"exception [{d['severity']}] {d['kind']}"
    if ev in ("run.succeeded", "run.failed"):
        return f"run {e['run_id']} {ev.split('.')[1]}"
    return None
