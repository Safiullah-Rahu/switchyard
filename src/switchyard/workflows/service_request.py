"""service_request — existing customer message → logged case, team alert, approved reply."""

from __future__ import annotations

from ..engine import Done, StepContext, StepDef, WorkflowDef


async def log_case(ctx: StepContext) -> Done:
    case = ctx.input["case"]
    contact = ctx.act(
        "crm.upsert_contact",
        {
            "email": case["customer_email"],
            "name": case.get("customer_name"),
            "company": None,
            "phone": None,
            "segment": None,
        },
        slot="contact",
    )
    contact_id = contact["receipt"]["external_id"]
    ctx.act(
        "crm.add_note",
        {
            "object_id": contact_id,
            "body": f"[{case['category']}/{case['urgency']}] {case['summary']}",
            "author": "switchyard",
        },
        slot="case_note",
    )
    return Done({"contact_id": contact_id})


async def alert_team(ctx: StepContext) -> Done:
    case = ctx.input["case"]
    channel = "#service-urgent" if case["urgency"] == "high" else "#service"
    ctx.act(
        "chat.post",
        {
            "channel": channel,
            "text": f"{case['category'].title()} case from {case.get('customer_name') or case['customer_email']}: {case['summary']}",
        },
        slot="alert",
    )
    return Done({"channel": channel})


async def acknowledge(ctx: StepContext) -> Done:
    case = ctx.input["case"]
    sent = ctx.act(
        "email.send",
        {"to": [case["customer_email"]], "cc": [], "subject": case["reply_subject"], "body": case["reply_body"]},
        slot="ack",
    )
    return Done({"message_id": sent["receipt"]["details"]["message_id"]})


WORKFLOW = WorkflowDef(
    name="service_request",
    version=1,
    description="Log a service case, alert the team at once, send the customer an approved reply.",
    steps=[
        StepDef("log_case", log_case, description="CRM contact + case note"),
        StepDef("alert_team", alert_team, ("log_case",), description="Post to #service or #service-urgent"),
        StepDef("acknowledge", acknowledge, ("log_case",), description="Reply to the customer (approval required)"),
    ],
)
