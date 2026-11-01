"""lead_to_proposal — the pilot workflow.

    check_existing ─► crm_record ─────────────┬─► schedule_survey ─┐
    estimate ─► draft_proposal ⇄ review ──────┴─► proposal_document ┴─► send_proposal ─► crm_stage ─► notify_team

Everything internal runs on its own. One thing waits for a person: the email
that carries a priced proposal to the customer (external communication *and* a
binding offer). An open deal for the same contact pauses for a decision.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from ..canonical import payload_hash
from ..checks import check_proposal
from ..engine import Done, Rewind, StepContext, StepDef, StepFatal, StepRetry, WorkflowDef
from ..estimator import Estimate, EstimateInputs, estimate
from ..sandbox import SURVEY_CALENDAR
from .common import estimate_for_agent

OPEN_STAGES = {"new", "qualified", "survey_booked", "proposal_sent"}


def _lead(ctx: StepContext) -> dict:
    return ctx.input["lead"]


def _who(lead: dict) -> str:
    return lead.get("company") or lead["contact_name"]


async def check_existing(ctx: StepContext) -> Done:
    lead = _lead(ctx)
    open_deals = [d for d in ctx.reads.crm_deals_for_email(lead["email"]) if d["stage"] in OPEN_STAGES]
    if not open_deals:
        return Done({"mode": "new"})
    deal = open_deals[0]
    choice = ctx.decide(
        "existing_deal",
        f"{lead['contact_name']} already has an open deal “{deal['title']}” (stage {deal['stage']}, owner {deal['owner']}). "
        "Attach this enquiry to it, or open a separate deal?",
        ["attach_to_existing", "open_new_deal"],
        {"deal_id": deal["id"], "owner": deal["owner"]},
    )
    return Done(
        {"mode": "attach" if choice == "attach_to_existing" else "new", "deal_id": deal["id"], "decision": choice}
    )


async def crm_record(ctx: StepContext) -> Done:
    lead = _lead(ctx)
    contact = ctx.act(
        "crm.upsert_contact",
        {
            "email": lead["email"],
            "name": lead["contact_name"],
            "company": lead.get("company"),
            "phone": lead.get("phone"),
            "segment": lead["segment"],
        },
        slot="contact",
    )
    contact_id = contact["receipt"]["external_id"]
    existing = ctx.output_of("check_existing")
    if existing["mode"] == "attach":
        deal_id = existing["deal_id"]
        ctx.act(
            "crm.add_note",
            {
                "object_id": deal_id,
                "body": f"New enquiry attached by Switchyard: {lead['rationale']}",
                "author": "switchyard",
            },
            slot="attach_note",
        )
    else:
        currency = ctx.settings.estimator["regions"][lead["region"]]["currency"]
        products = " + ".join(p.replace("_", " ") for p in lead["interested_in"])
        deal = ctx.act(
            "crm.create_deal",
            {
                "contact_id": contact_id,
                "title": f"{_who(lead)} — {products}",
                "stage": "new",
                "currency": currency,
                "owner": "maya.patel",
            },
            slot="deal",
        )
        deal_id = deal["receipt"]["external_id"]
    return Done(
        {
            "contact_id": contact_id,
            "deal_id": deal_id,
            "contact_created": bool(contact["receipt"]["details"].get("created")),
        }
    )


async def estimate_step(ctx: StepContext) -> Done:
    lead = _lead(ctx)
    inputs = EstimateInputs(
        segment=lead["segment"],
        region=lead["region"],
        annual_consumption_kwh=lead.get("annual_consumption_kwh"),
        roof_area_m2=lead.get("roof_area_m2"),
        include_battery="battery" in lead["interested_in"],
    )
    try:
        est = estimate(inputs, ctx.settings.estimator)
    except ValueError as exc:
        raise StepFatal(
            f"cannot size a system: {exc}", kind="insufficient_data", details={"missing": lead["missing_information"]}
        ) from None
    return Done(est.model_dump())


async def draft_proposal(ctx: StepContext) -> Done:
    lead = _lead(ctx)
    est = Estimate(**ctx.output_of("estimate"))
    prompt = f"Write the proposal for this lead.\n<lead>{json.dumps(lead)}</lead>"
    if ctx.notes:
        findings = "\n".join(f"- {n['note']}" for n in ctx.notes)
        prompt += (
            f"\n<review_findings>\nThe previous draft was rejected. Fix all of these:\n{findings}\n</review_findings>"
        )
    outcome = await ctx.run_agent("proposal_writer", prompt, estimate=estimate_for_agent(est))
    return Done({"draft": outcome.output.model_dump(), "revision": len(ctx.notes)})


async def review(ctx: StepContext) -> Done | Rewind:
    lead = _lead(ctx)
    est = Estimate(**ctx.output_of("estimate"))
    draft = ctx.output_of("draft_proposal")["draft"]
    template = ctx.reads.docs_template(f"{lead['segment']}-solar-proposal") or ctx.reads.docs_template(
        "commercial-solar-proposal"
    )

    # 1. Deterministic checks decide first and cannot be overruled.
    report = check_proposal(draft, est, template["required_disclaimer"])
    if not report.passed:
        return Rewind(
            "draft_proposal",
            report.summary(),
            {"source": "numeric_checks", "findings": [f.__dict__ for f in report.findings]},
        )

    # 2. An independent model reviews tone and claims (separate agent, no write tools).
    outcome = await ctx.run_agent(
        "reviewer", f"Review this draft proposal.\n<draft>{json.dumps(draft)}</draft>", estimate=estimate_for_agent(est)
    )
    verdict = outcome.output
    if not verdict.approve:
        blockers = [i.description for i in verdict.issues if i.severity == "blocker"] or [verdict.summary]
        return Rewind(
            "draft_proposal",
            "; ".join(blockers),
            {"source": "reviewer", "issues": [i.model_dump() for i in verdict.issues]},
        )
    return Done(
        {"checks": report.as_dict(), "reviewer": verdict.model_dump(), "approved_draft_hash": payload_hash(draft)}
    )


async def schedule_survey(ctx: StepContext) -> Done:
    lead = _lead(ctx)
    crm = ctx.output_of("crm_record")
    today = date.fromtimestamp(ctx.engine.clock()).isoformat()
    outcome = await ctx.run_agent(
        "survey_scheduler",
        f"Choose a site-survey slot.\n<preference>{lead.get('survey_preference') or ''}</preference>\n<today>{today}</today>",
    )
    slot = outcome.output
    free = {(s["start"], s["end"]) for s in ctx.reads.calendar_free_slots(SURVEY_CALENDAR, today, 12, 90)}
    if (slot.start, slot.end) not in free:
        raise StepRetry(f"scheduler chose {slot.start}–{slot.end}, which is not a free slot", kind="agent_error")
    event = ctx.act(
        "calendar.create_event",
        {
            "calendar": SURVEY_CALENDAR,
            "title": f"Site survey — {_who(lead)}",
            "start": slot.start,
            "end": slot.end,
            "attendees": [],
            "description": f"Deal {crm['deal_id']}. Site: {lead.get('site_address') or 'see CRM'}. Tentative until the customer confirms.",
        },
        slot="survey_hold",
    )
    held = event["payload"]  # what was actually written wins over a re-run's choice
    return Done(
        {"start": held["start"], "end": held["end"], "event_id": event["receipt"]["external_id"], "reason": slot.reason}
    )


async def proposal_document(ctx: StepContext) -> Done:
    draft = ctx.output_of("draft_proposal")["draft"]
    if payload_hash(draft) != ctx.output_of("review")["approved_draft_hash"]:
        raise StepFatal("the draft changed after it passed review", kind="integrity_error")
    deal_id = ctx.output_of("crm_record")["deal_id"]
    doc = ctx.act(
        "docs.create",
        {"title": draft["title"], "folder": f"Proposals/{deal_id}", "body": draft["body_markdown"], "shared_with": []},
        slot="proposal_doc",
    )
    return Done({"doc_id": doc["receipt"]["external_id"], "url": doc["receipt"]["details"]["url"]})


async def send_proposal(ctx: StepContext) -> Done:
    lead = _lead(ctx)
    est = Estimate(**ctx.output_of("estimate"))
    draft = ctx.output_of("draft_proposal")["draft"]
    doc = ctx.output_of("proposal_document")
    survey = ctx.output_of("schedule_survey")
    when = datetime.fromisoformat(survey["start"]).strftime("%A %d %B at %H:%M")
    body = (
        f"{draft['email_body']}\n\nYour proposal: {doc['url']}\n\n"
        f"We have pencilled in a site survey for {when}. Reply to this email if that time does not suit you.\n\n"
        "Kind regards,\nMaya Patel\nDemo Energy Co"
    )
    sent = ctx.act(
        "email.send",
        {
            "to": [lead["email"]],
            "cc": [],
            "subject": draft["email_subject"],
            "body": body,
            "quote_total": est.capex,
            "currency": est.currency,
        },
        slot="proposal_email",
    )
    check = ctx.engine.approvals.check(sent)
    return Done(
        {
            "message_id": sent["receipt"]["details"]["message_id"],
            "approved_by": check.approval["decided_by"] if check.approval else None,
        }
    )


async def crm_stage(ctx: StepContext) -> Done:
    crm = ctx.output_of("crm_record")
    est = Estimate(**ctx.output_of("estimate"))
    sent = ctx.output_of("send_proposal")
    doc = ctx.output_of("proposal_document")
    survey = ctx.output_of("schedule_survey")
    ctx.act(
        "crm.update_deal_stage",
        {"deal_id": crm["deal_id"], "stage": "proposal_sent", "amount": est.capex},
        slot="stage",
    )
    ctx.act(
        "crm.add_note",
        {
            "object_id": crm["deal_id"],
            "body": f"Proposal sent ({doc['url']}), approved by {sent['approved_by']}. Survey held {survey['start']}. Message {sent['message_id']}.",
            "author": "switchyard",
        },
        slot="sent_note",
    )
    return Done({"stage": "proposal_sent"})


async def notify_team(ctx: StepContext) -> Done:
    lead = _lead(ctx)
    est = Estimate(**ctx.output_of("estimate"))
    survey = ctx.output_of("schedule_survey")
    crm = ctx.output_of("crm_record")
    text = (
        f"Proposal sent to {lead['contact_name']} ({_who(lead)}): {est.system_kwp} kWp, {est.symbol}{est.capex:,}, "
        f"payback {est.payback_years} years. Survey pencilled in {survey['start']}. Deal {crm['deal_id']}."
    )
    ctx.act("chat.post", {"channel": "#sales", "text": text}, slot="sent_notice")
    return Done({"posted": True})


WORKFLOW = WorkflowDef(
    name="lead_to_proposal",
    version=1,
    description="Qualified lead → CRM record, estimate, reviewed proposal, survey hold, approved email.",
    steps=[
        StepDef("check_existing", check_existing, description="Open deal for this contact? Ask a person."),
        StepDef("crm_record", crm_record, ("check_existing",), description="Upsert contact, open deal"),
        StepDef("estimate", estimate_step, description="Deterministic sizing and savings"),
        StepDef("draft_proposal", draft_proposal, ("estimate",), description="Proposal Writer agent"),
        StepDef("review", review, ("draft_proposal",), description="Numeric checks + Independent Reviewer agent"),
        StepDef(
            "schedule_survey", schedule_survey, ("crm_record",), description="Survey Scheduler agent + calendar hold"
        ),
        StepDef(
            "proposal_document", proposal_document, ("review", "crm_record"), description="Create the proposal document"
        ),
        StepDef(
            "send_proposal",
            send_proposal,
            ("proposal_document", "schedule_survey"),
            description="Email the customer (approval required)",
        ),
        StepDef("crm_stage", crm_stage, ("send_proposal",), description="Deal → proposal_sent, audit note"),
        StepDef("notify_team", notify_team, ("crm_stage",), description="Post to #sales"),
    ],
)
