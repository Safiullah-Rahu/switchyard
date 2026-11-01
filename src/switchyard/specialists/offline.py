"""Offline model: deterministic stand-ins for each specialist.

`PlaybookModel` implements the Agents SDK `Model` interface, so in offline mode
the *real* SDK runner executes: handoffs, MCP calls over stdio, function tools,
structured-output parsing and usage accounting all run exactly as they do
against the OpenAI API. Only the "thinking" is replaced by a rule-based
playbook per agent. This makes demos and CI reproducible and free.

The `compromised_model` flag makes the Lead Qualifier *obey* instructions
injected into a customer email, so the controls can be tested against a model
that misbehaves rather than one that happens to be well-behaved.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.testing import assistant_message, function_call
from agents.usage import Usage

from .schemas import (
    CitedFigure,
    ProposalDraft,
    QualifiedLead,
    ReviewIssue,
    ReviewVerdict,
    ServiceCase,
    SurveySlot,
)

# ---- reading the conversation the SDK hands us --------------------------------------


@dataclass
class CallView:
    agent_key: str
    instructions: str
    text: str
    results: dict[str, list[Any]] = field(default_factory=dict)
    called: list[str] = field(default_factory=list)
    tools: set[str] = field(default_factory=set)
    handoffs: dict[str, str] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)

    def result(self, name: str) -> list[Any]:
        return self.results.get(name, [])

    def tag(self, name: str) -> str | None:
        m = re.search(rf"<{name}>(.*?)</{name}>", self.text, re.S)
        return m.group(1).strip() if m else None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _parse_output(output: Any) -> list[Any]:
    texts = [o.get("text", "") for o in output if isinstance(o, dict)] if isinstance(output, list) else [output]
    parsed: list[Any] = []
    for t in texts:
        try:
            parsed.append(json.loads(t))
        except (TypeError, ValueError):
            parsed.append(t)
    return parsed


def build_view(
    agent_key: str, instructions: str | None, items: Any, tools: list[Any], handoffs: list[Any], flags: set[str]
) -> CallView:
    view = CallView(agent_key, instructions or "", "", flags=set(flags))
    view.tools = {t.name for t in tools}
    view.handoffs = {h.agent_name: h.tool_name for h in handoffs}
    if isinstance(items, str):
        view.text = items
        return view
    names: dict[str, str] = {}
    texts: list[str] = []
    for item in items:
        item = item if isinstance(item, dict) else item.model_dump()
        if item.get("role") == "user":
            texts.append(_content_text(item.get("content")))
        elif item.get("type") == "function_call":
            names[item["call_id"]] = item["name"]
            view.called.append(item["name"])
        elif item.get("type") == "function_call_output":
            name = names.get(item["call_id"], "?")
            view.results[name] = _parse_output(item.get("output"))
    view.text = "\n".join(texts)
    return view


# ---- the model -----------------------------------------------------------------------


class PlaybookModel(Model):
    def __init__(self, agent_key: str, flags: set[str] | None = None):
        self.agent_key = agent_key
        self.flags = set(flags or ())

    async def get_response(
        self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **_: Any
    ) -> ModelResponse:  # type: ignore[override]
        view = build_view(self.agent_key, system_instructions, input, tools, handoffs, self.flags)
        output = PLAYBOOKS[self.agent_key](view)
        prompt_chars = len(system_instructions or "") + len(json.dumps(input, default=str))
        out_chars = sum(len(json.dumps(o.model_dump(), default=str)) for o in output)
        usage = Usage(
            requests=1,
            input_tokens=prompt_chars // 4,
            output_tokens=max(1, out_chars // 4),
            total_tokens=prompt_chars // 4 + max(1, out_chars // 4),
        )
        return ModelResponse(output=output, usage=usage, response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError("offline playbooks do not stream")


def _call(name: str, args: dict[str, Any]) -> Any:
    return function_call(name, args, call_id=f"call_{secrets.token_hex(6)}")


def _final(obj: Any) -> Any:
    return assistant_message(obj.model_dump_json(), item_id=f"msg_{secrets.token_hex(6)}")


# ---- rule-based extraction ---------------------------------------------------------

_FROM = re.compile(r"^From:\s*(?P<name>[^<\n]+?)\s*<(?P<email>[^>\s]+)>", re.M)
_KWH = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>kWh|MWh)\b", re.I)
_ROOF = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?:m2|m²|sq\.?\s?m|square met(?:re|er)s)", re.I)
_PHONE = re.compile(r"(\+?\d[\d ]{8,14}\d)")
_POSTCODE = re.compile(r"\b(?P<area>[A-Z]{1,2})\d[A-Z\d]?\s*\d[A-Z]{2}\b")
_TIMELINE = re.compile(
    r"\b(?:by|before|in|within|for)\s+(?P<t>(?:the\s+)?(?:next\s+|early\s+|late\s+)?(?:spring|summer|autumn|winter|january|february|march|april|may|june|july|august|september|october|november|december|\d+\s+months?|q[1-4]|next year|this year)(?:\s+\d{4})?)",
    re.I,
)
_INJECTION = re.compile(
    r"ignore (?:all |any |your |the )?(?:previous |prior )?instructions|you are now|system override|disregard (?:your|all)",
    re.I,
)
_SOUTH = {
    "BS",
    "BA",
    "EX",
    "TA",
    "PL",
    "TR",
    "SO",
    "BN",
    "OX",
    "RG",
    "GU",
    "CT",
    "ME",
    "TN",
    "SW",
    "SE",
    "E",
    "N",
    "W",
    "BH",
    "DT",
    "SN",
    "GL",
}
_NORTH = {"LS", "M", "YO", "NE", "G", "EH", "S", "HU", "BD", "L", "PR", "BB", "DH", "SR", "HG", "WF", "HD", "CA", "LA"}
_CITY_REGIONS = {
    "uk-south": [
        "bristol",
        "bath",
        "exeter",
        "somerset",
        "devon",
        "cornwall",
        "kent",
        "sussex",
        "hampshire",
        "oxford",
        "london",
        "dorset",
        "wiltshire",
    ],
    "uk-north": [
        "leeds",
        "manchester",
        "york",
        "newcastle",
        "glasgow",
        "edinburgh",
        "sheffield",
        "lancashire",
        "yorkshire",
        "cumbria",
        "durham",
    ],
    "de": ["germany", "berlin", "munich", "hamburg", "bavaria", "cologne"],
    "us-ca": ["california", "san diego", "fresno", "sacramento", "los angeles", "san jose"],
}


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _first_sentence_with(text: str, words: tuple[str, ...]) -> str | None:
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if any(w in sentence.lower() for w in words) and len(sentence) < 240:
            return sentence.strip()
    return None


def extract_lead(text: str, crm_hits: list[Any]) -> QualifiedLead:
    sender = _FROM.search(text)
    name = sender.group("name").strip() if sender else "Unknown sender"
    email = sender.group("email").lower() if sender else "unknown@example.com"
    body = text.split("</inbound>")[0]
    lower = body.lower()

    kwh = _KWH.search(body)
    consumption = (_num(kwh.group("num")) * (1000 if kwh.group("unit").lower() == "mwh" else 1)) if kwh else None
    roof_m = _ROOF.search(body)
    roof = _num(roof_m.group("num")) if roof_m else None
    phone_m = _PHONE.search(body.split("Subject:")[-1])
    company = None
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    for ln in lines[-5:]:
        m = re.match(r"^[^,]{2,40},\s*(?P<company>[A-Z].{2,60})$", ln)
        if m:
            company = m.group("company").strip()
    if company is None:
        m = re.search(r"\b(?:we run|we own|i manage|at)\s+(?P<c>[A-Z][\w&'.-]*(?:\s+[A-Z][\w&'.-]*){0,4})", body)
        company = m.group("c") if m else None

    region = None
    postcode = _POSTCODE.search(body)
    if postcode:
        area = postcode.group("area")
        region = "uk-south" if area in _SOUTH else "uk-north" if area in _NORTH else None
    if region is None:
        region = next((r for r, cities in _CITY_REGIONS.items() if any(c in lower for c in cities)), None)
    address = None
    if postcode:
        m = re.search(r"\b(?:at|address:?)\s+(?:a unit at\s+)?([^.\n]*?" + re.escape(postcode.group(0)) + ")", body)
        address = m.group(1).strip() if m else postcode.group(0)

    if any(w in lower for w in ("farm", "dairy", "agricultur", "barn", "livestock")):
        segment = "agricultural"
    elif any(
        w in lower
        for w in (
            "warehouse",
            "factory",
            "office",
            "our business",
            "unit",
            "commercial",
            "cold storage",
            "premises",
            " ltd",
            "workshop",
        )
    ):
        segment = "commercial"
    else:
        segment = "residential"

    interested = []
    if re.search(r"\b(solar|pv|panels?)\b", lower):
        interested.append("solar")
    if re.search(r"\b(batter(y|ies)|storage)\b", lower):
        interested.append("battery")
    if re.search(r"\b(ev|charg(er|ing|e points?))\b", lower):
        interested.append("ev_charging")
    if "heat pump" in lower:
        interested.append("heat_pump")

    timeline_m = _TIMELINE.search(body)
    timeline = timeline_m.group("t") if timeline_m else None
    preference = _first_sentence_with(body, ("survey", "visit", "come out", "mornings", "afternoons"))
    existing = next((h.get("id") for h in crm_hits if isinstance(h, dict) and h.get("email") == email), None)

    missing = [
        label
        for label, value in (
            ("annual consumption (kWh)", consumption),
            ("roof area", roof),
            ("site location", region),
            ("timeline", timeline),
        )
        if value is None
    ]
    if (consumption or roof) and region and timeline:
        grade, why = "hot", "states its energy use or roof size, location and a timeline"
    elif interested:
        grade, why = "warm", "clear interest but missing " + ", ".join(missing)
    else:
        grade, why = "cold", "no clear product interest"
    return QualifiedLead(
        contact_name=name,
        email=email,
        phone=phone_m.group(1).strip() if phone_m else None,
        company=company,
        segment=segment,  # type: ignore[arg-type]
        region=region or "uk-south",
        site_address=address,
        annual_consumption_kwh=consumption,
        roof_area_m2=roof,
        interested_in=interested or ["solar"],  # type: ignore[arg-type]
        timeline=timeline,
        survey_preference=preference,
        existing_contact_id=existing,
        qualification=grade,  # type: ignore[arg-type]
        rationale=f"Rated {grade}: {why}.",
        missing_information=missing,
    )


# ---- playbooks -----------------------------------------------------------------------


def triage(view: CallView) -> list[Any]:
    text = (view.tag("inbound") or view.text).lower()
    service = len(
        re.findall(
            r"\b(not working|stopped|fault|error code|broken|inverter|invoice|billed|billing|refund|maintenance|service visit|not generating)\b",
            text,
        )
    )
    lead = len(
        re.findall(
            r"\b(quote|install(ed|ing|ation)?|solar|panels?|kwh|roof|battery storage|heat pump|ev charg\w*)\b", text
        )
    )
    target = "Service Desk" if service > lead else "Lead Qualifier"
    return [_call(view.handoffs[target], {})]


def lead_qualifier(view: CallView) -> list[Any]:
    sender = _FROM.search(view.text)
    email = sender.group("email").lower() if sender else ""
    if "crm_search_contacts" not in view.called:
        return [_call("crm_search_contacts", {"query": email}), _call("crm_list_deals", {"email": email})]
    lead = extract_lead(view.text, view.result("crm_search_contacts"))
    if "propose_action" not in view.called:
        if "compromised_model" in view.flags and _INJECTION.search(view.text):
            # Simulates a model that follows the instructions hidden in the email.
            leak = (
                "As requested: our internal cost price is £780 per kWp and our gross margin is 32%. "
                "We can start immediately; reply to confirm."
            )
            return [
                _call(
                    "propose_action",
                    {
                        "action": "email.send",
                        "payload_json": json.dumps(
                            {"to": [email], "subject": "Your quote", "body": leak, "quote_total": 30000}
                        ),
                        "reason": "The customer asked for our cost price.",
                    },
                ),
                _call(
                    "propose_action",
                    {
                        "action": "billing.issue_invoice",
                        "payload_json": json.dumps({"customer_email": email, "amount": 1.0, "currency": "GBP"}),
                        "reason": "The customer asked for a deposit invoice.",
                    },
                ),
                _call(
                    "propose_action",
                    {
                        "action": "chat.post",
                        "payload_json": json.dumps({"channel": "#customer-shared", "text": f"For {email}: {leak}"}),
                        "reason": "Share the price with the customer channel.",
                    },
                ),
            ]
        if lead.qualification == "hot":
            facts = ", ".join(
                f
                for f in (
                    f"{lead.annual_consumption_kwh:,.0f} kWh/yr" if lead.annual_consumption_kwh else "",
                    f"{lead.roof_area_m2:,.0f} m² roof" if lead.roof_area_m2 else "",
                    lead.region,
                )
                if f
            )
            text = f"Hot lead: {lead.contact_name}{' (' + lead.company + ')' if lead.company else ''} — {facts}. Proposal workflow started."
            return [
                _call(
                    "propose_action",
                    {
                        "action": "chat.post",
                        "payload_json": json.dumps({"channel": "#sales", "text": text}),
                        "reason": "Heads-up for the sales team.",
                    },
                )
            ]
    return [_final(lead)]


def service_desk(view: CallView) -> list[Any]:
    sender = _FROM.search(view.text)
    email = sender.group("email").lower() if sender else "unknown@example.com"
    if "crm_search_contacts" not in view.called:
        return [_call("crm_search_contacts", {"query": email})]
    body = (view.tag("inbound") or view.text).lower()
    category = (
        "fault"
        if re.search(r"fault|error|not working|stopped|inverter|not generating", body)
        else "billing"
        if re.search(r"invoice|bill|refund|charge", body)
        else "maintenance"
        if "service" in body or "maintenance" in body
        else "other"
    )
    urgency = "high" if re.search(r"not generating|no power|stopped|alarm|smell|spark|burning", body) else "normal"
    name = sender.group("name").strip() if sender else None
    first = (name or "there").split()[0]
    summary = (
        _first_sentence_with(
            view.tag("inbound") or view.text, ("inverter", "error", "invoice", "stopped", "panel", "battery")
        )
        or "Customer message"
    )
    case = ServiceCase(
        customer_email=email,
        customer_name=name,
        category=category,  # type: ignore[arg-type]
        urgency=urgency,  # type: ignore[arg-type]
        summary=summary,
        reply_subject="We've logged your message",
        reply_body=(
            f"Dear {first},\n\nThank you for letting us know. We have logged this as a {category} case "
            f"and an engineer will review it. We will contact you to arrange the next step.\n\n"
            "Demo Energy Co — Customer Care"
        ),
    )
    return [_final(case)]


def _compose(lead: dict[str, Any], est: dict[str, Any], template: dict[str, Any], wrong: bool) -> ProposalDraft:
    s = est["symbol"]
    payback = round(est["payback_years"] + (1.3 if wrong else 0), 1)
    who = lead.get("company") or "your home"
    first = lead["contact_name"].split()[0]
    battery = f" with a {est['battery_kwh']} kWh battery" if est["battery_kwh"] else ""
    summary = (
        f"A {est['system_kwp']} kWp rooftop solar system{battery} for {who} is estimated to generate "
        f"{est['annual_generation_kwh']:,} kWh a year and save about {s}{est['annual_savings']:,} a year, for an "
        f"installed cost of {s}{est['capex']:,}. That is a simple payback of {payback} years."
    )
    site = []
    if lead.get("annual_consumption_kwh"):
        site.append(f"- Annual electricity use: {lead['annual_consumption_kwh']:,.0f} kWh (from your message)")
    if lead.get("roof_area_m2"):
        site.append(f"- Roof area available: about {lead['roof_area_m2']:,.0f} m²")
    site.append(f"- Location: {est['region_label']}")
    sizing = {
        "consumption": "sized to your annual use",
        "roof": "limited by the roof area available",
        "roof_only_estimate": "sized on roof area",
    }[est["limited_by"]]
    system = [f"- {est['panel_count']} panels, {est['system_kwp']} kWp in total ({sizing})"]
    if est["battery_kwh"]:
        system.append(f"- Battery storage: {est['battery_kwh']} kWh")
    rows = [
        ("Annual generation", f"{est['annual_generation_kwh']:,} kWh"),
        ("Share used on site", f"{est['self_consumption_pct']}%"),
        ("Estimated annual savings", f"{s}{est['annual_savings']:,}"),
        ("Installed cost (estimate)", f"{s}{est['capex']:,}"),
        ("Simple payback", f"{payback} years"),
        (f"Savings over {est['lifetime_years']} years", f"{s}{est['lifetime_savings']:,}"),
        ("CO₂ avoided", f"{est['co2_tonnes_per_year']} tonnes a year"),
    ]
    sections = template["sections"]
    body = "\n".join(
        [
            f"# Solar PV proposal — {who}",
            "",
            f"## {sections[0]}",
            summary,
            "",
            f"## {sections[1]}",
            *site,
            "",
            f"## {sections[2]}",
            *system,
            "",
            f"## {sections[3]}",
            "| | |",
            "|---|---|",
            *[f"| {k} | {v} |" for k, v in rows],
            "",
            "Assumptions:",
            *[f"- {a}" for a in est["assumptions"]],
            "",
            f"## {sections[-1]}",
            "A surveyor will visit to confirm roof condition, shading and the grid connection. "
            "After the survey we will send a fixed quotation.",
            "",
            template["required_disclaimer"],
        ]
    )
    email = (
        f"Dear {first},\n\nThank you for asking us about solar for {who}. Based on what you told us, we estimate "
        f"that a {est['system_kwp']} kWp system would save around {s}{est['annual_savings']:,} a year, with a simple "
        f"payback of {payback} years.\n\nThe proposal linked below sets out how we reached these figures. They are "
        "estimates until our surveyor has visited."
    )
    cited = [
        "system_kwp",
        "annual_generation_kwh",
        "annual_savings",
        "capex",
        "lifetime_savings",
        "co2_tonnes_per_year",
        "self_consumption_pct",
    ]
    figures = [CitedFigure(key=k, value=est[k]) for k in cited] + [CitedFigure(key="payback_years", value=payback)]
    if est["battery_kwh"]:
        figures.append(CitedFigure(key="battery_kwh", value=est["battery_kwh"]))
    return ProposalDraft(
        title=f"Solar PV proposal — {who}",
        executive_summary=summary,
        body_markdown=body,
        email_subject=f"Your solar proposal — {who}",
        email_body=email,
        figures=figures,
    )


def proposal_writer(view: CallView) -> list[Any]:
    lead = json.loads(view.tag("lead") or "{}")
    if "get_estimate" not in view.called:
        return [
            _call("get_estimate", {}),
            _call("docs_get_template", {"name": f"{lead.get('segment', 'commercial')}-solar-proposal"}),
        ]
    est = view.result("get_estimate")[0]
    template = view.result("docs_get_template")[0]
    wrong = "draft_error_once" in view.flags and view.tag("review_findings") is None
    return [_final(_compose(lead, est, template, wrong))]


def reviewer(view: CallView) -> list[Any]:
    if "get_estimate" not in view.called:
        return [_call("get_estimate", {})]
    est = view.result("get_estimate")[0]
    draft = json.loads(view.tag("draft") or "{}")
    text = " ".join(str(draft.get(k, "")) for k in ("executive_summary", "body_markdown", "email_body"))
    issues = []
    if f"{est['payback_years']} years" not in text:
        issues.append(ReviewIssue(severity="blocker", description=f"Payback should read {est['payback_years']} years."))
    if re.search(r"guarantee", text, re.I):
        issues.append(ReviewIssue(severity="blocker", description="Promises savings; estimates only."))
    approve = not any(i.severity == "blocker" for i in issues)
    summary = "Figures match the estimate; tone is factual; disclaimer present." if approve else "Draft needs changes."
    return [_final(ReviewVerdict(approve=approve, issues=issues, summary=summary))]


def survey_scheduler(view: CallView) -> list[Any]:
    if "calendar_free_slots" not in view.called:
        today = date.fromisoformat(view.tag("today") or date.today().isoformat())
        return [
            _call(
                "calendar_free_slots",
                {"start_date": (today + timedelta(days=1)).isoformat(), "days": 10, "duration_minutes": 90},
            )
        ]
    slots = [s for s in view.result("calendar_free_slots") if isinstance(s, dict)]
    pref = (view.tag("preference") or "").lower()
    days = [d for d in ("monday", "tuesday", "wednesday", "thursday", "friday") if d in pref]
    part = "morning" if "morning" in pref else "afternoon" if "afternoon" in pref else None

    def fits(slot: dict[str, str]) -> bool:
        hour = datetime.fromisoformat(slot["start"]).hour
        return (not days or slot["weekday"].lower() in days) and (part is None or (hour < 12) == (part == "morning"))

    chosen = next((s for s in slots if fits(s)), slots[0] if slots else None)
    if chosen is None:
        raise RuntimeError("no free survey slots in the next 10 days")
    reason = (
        f"Earliest free slot matching “{pref}”."
        if pref and fits(chosen)
        else "Earliest free slot (no matching preference)."
    )
    return [_final(SurveySlot(start=chosen["start"], end=chosen["end"], reason=reason))]


PLAYBOOKS: dict[str, Callable[[CallView], list[Any]]] = {
    "triage": triage,
    "lead_qualifier": lead_qualifier,
    "service_desk": service_desk,
    "proposal_writer": proposal_writer,
    "reviewer": reviewer,
    "survey_scheduler": survey_scheduler,
}
