"""Function tools the specialists can call, and the run context they receive."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agents import function_tool
from agents.tool_context import ToolContext

from ..canonical import payload_hash
from ..policy import PolicyViolation

# Display name → policy key. The policy file grants permissions by key.
AGENT_KEYS = {
    "Triage": "triage",
    "Lead Qualifier": "lead_qualifier",
    "Service Desk": "service_desk",
    "Proposal Writer": "proposal_writer",
    "Survey Scheduler": "survey_scheduler",
    "Independent Reviewer": "reviewer",
}


@dataclass
class AgentContext:
    """Passed to every tool call. Holds no credentials and no handle that can write anywhere."""

    engine: Any
    run_id: str
    step: str
    proposals: list[dict[str, Any]]
    estimate: dict[str, Any] | None = None
    flags: set[str] = field(default_factory=set)
    refused: list[dict[str, Any]] = field(default_factory=list)


@function_tool
def get_estimate(ctx: ToolContext[AgentContext]) -> str:
    """Return the deterministic solar/battery estimate for this lead: every figure you may quote,
    with its unit, plus the assumptions it was priced with."""
    est = ctx.context.estimate
    if est is None:
        return json.dumps({"error": "no estimate is available in this step"})
    return json.dumps(est)


@function_tool
def propose_action(ctx: ToolContext[AgentContext], action: str, payload_json: str, reason: str) -> str:
    """Ask the Switchyard engine to perform a side effect. You cannot perform actions yourself:
    the engine checks the action against company policy and runs it later, possibly after a
    person approves it.

    Args:
        action: A registered action name, for example "chat.post" or "crm.add_note".
        payload_json: The action's fields as a JSON object, e.g. {"channel": "#sales", "text": "..."}.
        reason: One sentence on why this action is needed.
    """
    actx = ctx.context
    engine = actx.engine
    agent_key = AGENT_KEYS.get(ctx.agent.name if ctx.agent else "", "unknown")
    try:
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise ValueError("payload_json must be a JSON object")
        engine.policy.check_agent_may_propose(agent_key, action)
        classification = engine.policy.classify(action, payload)
    except (PolicyViolation, ValueError, KeyError) as exc:
        refusal = {"agent": agent_key, "action": action, "reason": str(exc), "agent_reason": reason}
        actx.refused.append(refusal)
        engine.audit.record(
            "agent.proposal_refused", actor=f"agent:{agent_key}", run_id=actx.run_id, subject=actx.step, data=refusal
        )
        return f"REFUSED by policy: {exc}. Nothing was done."
    actx.proposals.append(
        {
            "slot": f"agent:{agent_key}:{action}:{payload_hash(payload)[:12]}",
            "action": action,
            "payload": payload,
            "proposed_by": f"agent:{agent_key}",
        }
    )
    engine.audit.record(
        "agent.proposal_received",
        actor=f"agent:{agent_key}",
        run_id=actx.run_id,
        subject=actx.step,
        data={"action": action, "risk_class": classification.risk_class, "reason": reason},
    )
    if classification.requires_approval:
        return (
            f"Queued. Policy class {classification.risk_class}: a person must approve this before it "
            f"happens ({'; '.join(classification.reasons)}). Nothing has been sent."
        )
    return "Queued. It runs automatically once this step completes. Nothing has happened yet."
