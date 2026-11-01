"""survey_to_install — a project-operations workflow with no model calls at all.

Triggered when the surveyor's field app reports a completed survey. Not every
step needs an LLM: this one is plain orchestration, and the deposit invoice is
a binding commitment, so finance (or the sales lead) approves it.
"""

from __future__ import annotations

from ..engine import Done, StepContext, StepDef, StepFatal, WorkflowDef


def _deal(ctx: StepContext) -> dict:
    deal = ctx.reads.crm_get_deal(ctx.input["deal_id"])
    if deal is None:
        raise StepFatal(f"deal {ctx.input['deal_id']} does not exist", kind="tool_permanent")
    return deal


async def update_deal(ctx: StepContext) -> Done:
    survey = ctx.input["survey"]
    deal = _deal(ctx)
    stage = "survey_complete" if survey["roof_ok"] else "survey_issue"
    ctx.act("crm.update_deal_stage", {"deal_id": deal["id"], "stage": stage}, slot="stage")
    ctx.act(
        "crm.add_note",
        {"object_id": deal["id"], "body": f"Survey by {survey['surveyor']}: {survey['notes']}", "author": "switchyard"},
        slot="survey_note",
    )
    return Done({"stage": stage, "amount": deal["amount"], "currency": deal["currency"]})


async def handover_pack(ctx: StepContext) -> Done:
    survey = ctx.input["survey"]
    deal = _deal(ctx)
    body = "\n".join(
        [
            f"# Installation handover — {deal['title']}",
            f"- Confirmed system size: {survey['confirmed_kwp']} kWp",
            f"- Shading: {survey['shading']}",
            f"- Roof suitable: {'yes' if survey['roof_ok'] else 'no — see notes'}",
            f"- Surveyor notes: {survey['notes']}",
        ]
    )
    doc = ctx.act(
        "docs.create",
        {"title": f"Handover — {deal['title']}", "folder": f"Projects/{deal['id']}", "body": body, "shared_with": []},
        slot="handover",
    )
    return Done({"doc_id": doc["receipt"]["external_id"], "url": doc["receipt"]["details"]["url"]})


async def deposit_invoice(ctx: StepContext) -> Done:
    info = ctx.output_of("update_deal")
    if info["stage"] != "survey_complete":
        return Done({"skipped": "survey raised an issue; no invoice"})
    pct = ctx.input.get("deposit_pct", 20)
    amount = round(info["amount"] * pct / 100, 2)
    invoice = ctx.act(
        "billing.issue_invoice",
        {
            "customer_email": ctx.input["customer_email"],
            "amount": amount,
            "currency": info["currency"],
            "lines": [{"description": f"{pct}% deposit", "amount": amount}],
        },
        slot="deposit",
    )
    return Done({"invoice_id": invoice["receipt"]["external_id"], "amount": amount})


async def notify_ops(ctx: StepContext) -> Done:
    deal = _deal(ctx)
    pack = ctx.output_of("handover_pack")
    invoice = ctx.output_of("deposit_invoice")
    status = (
        f"deposit invoice {invoice['invoice_id']} issued" if "invoice_id" in invoice else invoice.get("skipped", "")
    )
    ctx.act(
        "chat.post",
        {"channel": "#installs", "text": f"{deal['title']}: survey done, handover pack {pack['url']}, {status}."},
        slot="ops_notice",
    )
    return Done({"posted": True})


WORKFLOW = WorkflowDef(
    name="survey_to_install",
    version=1,
    description="Completed survey → deal stage, handover pack, deposit invoice (approval), ops notice.",
    dedup=lambda i: f"survey:{i['deal_id']}",
    steps=[
        StepDef("update_deal", update_deal, description="Deal stage + survey note"),
        StepDef("handover_pack", handover_pack, ("update_deal",), description="Installation handover document"),
        StepDef(
            "deposit_invoice",
            deposit_invoice,
            ("update_deal",),
            description="Deposit invoice (binding commitment → approval)",
        ),
        StepDef("notify_ops", notify_ops, ("handover_pack", "deposit_invoice"), description="Post to #installs"),
    ],
)
