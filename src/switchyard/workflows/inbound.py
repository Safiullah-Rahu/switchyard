"""inbound_triage: one inbound message → the right workflow.

The Triage agent hands off (Agents SDK handoff) to the Lead Qualifier or the
Service Desk; whichever finishes returns a structured record, and this step
starts the matching child workflow. Child starts are deduplicated by business
key, so a re-run of this step never opens a second lead.
"""

from __future__ import annotations

from ..engine import Done, StepContext, StepDef, StepFatal, WorkflowDef
from ..specialists.schemas import QualifiedLead, ServiceCase
from .common import inbound_dedup, lead_dedup, render_inbound


async def route(ctx: StepContext) -> Done:
    message = ctx.input["message"]
    outcome = await ctx.run_agent("triage", render_inbound(message))
    flags = ctx.input.get("demo_flags", [])
    sender = message["from_email"].lower()

    if isinstance(outcome.output, QualifiedLead):
        lead = outcome.output.model_dump()
        # Identity comes from the envelope, not from anything the model read in the body.
        lead["email"] = sender
        if lead["region"] not in ctx.settings.estimator["regions"]:
            lead["missing_information"] = [*lead["missing_information"], f"unsupported region {lead['region']!r}"]
            lead["region"] = "uk-south"
        child, created = ctx.start_workflow(
            "lead_to_proposal", {"message": message, "lead": lead, "demo_flags": flags}, dedup_key=lead_dedup(lead)
        )
        return Done(
            {
                "route": "lead",
                "handled_by": outcome.agent,
                "child_run_id": child,
                "child_created": created,
                "qualification": lead["qualification"],
                "refused_proposals": outcome.refused,
            }
        )

    if isinstance(outcome.output, ServiceCase):
        case = outcome.output.model_dump()
        case["customer_email"] = sender
        child, created = ctx.start_workflow(
            "service_request", {"message": message, "case": case, "demo_flags": flags}, dedup_key=f"case:{ctx.run_id}"
        )
        return Done(
            {
                "route": "service",
                "handled_by": outcome.agent,
                "child_run_id": child,
                "child_created": created,
                "urgency": case["urgency"],
            }
        )

    raise StepFatal(f"triage finished without a structured record (last agent {outcome.agent})", kind="agent_error")


WORKFLOW = WorkflowDef(
    name="inbound_triage",
    version=1,
    description="Route an inbound email or web-form message to the lead or service workflow.",
    dedup=inbound_dedup,
    steps=[StepDef("route", route, description="Triage → Lead Qualifier | Service Desk (handoff)")],
)
