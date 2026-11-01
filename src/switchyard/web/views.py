"""The operations console: pages for people, and the form actions behind their buttons.

Pages are rendered on the server (Jinja2), so there is no build step and the
console works without JavaScript; a small script adds live refresh and theme
switching. Every action is a POST with a CSRF token, made by a signed-in
person whose roles are checked by the same services the API uses.
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..approvals import ApprovalError
from ..canonical import loads
from ..config import Settings
from ..engine import Engine
from . import present, session

TEMPLATES = Path(__file__).parent / "templates"


def _split_display(display: str) -> tuple[str, str]:
    name, _, rest = display.partition(" (")
    return name, rest.rstrip(")")


def build_router(engine: Engine, settings: Settings, lock: threading.RLock) -> APIRouter:
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters.update(present.FILTERS)
    templates.env.globals.update(risk=present.risk, money=present.money, now=engine.clock)
    router = APIRouter()
    secret = settings.approval_secret
    demo_sign_in = os.environ.get("SWITCHYARD_DEMO_SIGNIN", "1" if settings.mode == "offline" else "0") == "1"
    policy = engine.policy

    # ---- people -----------------------------------------------------------------------
    def person(principal: str | None) -> dict[str, Any] | None:
        if not principal:
            return None
        name, title = _split_display(policy.principal_display(principal))
        return {"id": principal, "name": name, "title": title, "roles": sorted(policy.principal_roles(principal))}

    def role_label(role: str) -> str:
        return role.replace("_", " ").capitalize()

    def holders(roles: list[str]) -> list[dict[str, Any]]:
        return [p for pid in policy.principals if (p := person(pid)) and set(p["roles"]) & set(roles)]

    def actor_label(actor: str) -> str:
        if actor.startswith("agent:"):
            return actor.split(":", 1)[1].replace("_", " ").capitalize() + " agent"
        if actor in policy.principals:
            return _split_display(policy.principal_display(actor))[0]
        if actor.startswith("workflow:"):
            return present.label_workflow(actor.split(":", 1)[1])
        if actor.startswith("webhook:"):
            return f"Webhook ({actor.split(':', 1)[1]})"
        return {"engine": "Engine", "policy": "Policy", "api": "API", "cli": "CLI", "demo": "Demo"}.get(actor, "Worker")

    # ---- view models ------------------------------------------------------------------
    def subject_of(run: dict[str, Any] | None) -> str:
        """Project runs are named after their deal; everything else after the customer."""
        if run and run.get("input", {}).get("deal_id") and engine.reads is not None:
            deal = engine.reads.crm_get_deal(run["input"]["deal_id"])
            if deal:
                return deal["title"]
        return present.run_subject(run)

    def pending_decisions() -> list[dict[str, Any]]:
        rows = engine.conn.execute("SELECT * FROM decisions WHERE status = 'pending' ORDER BY created_at").fetchall()
        return [decision_card(dict(r)) for r in rows]

    def decision_card(d: dict[str, Any]) -> dict[str, Any]:
        run = engine.get_run(d["run_id"])
        options = loads(d["options_json"], [])
        return {
            **d,
            "options": [{"value": o, "label": o.replace("_", " ").capitalize()} for o in options],
            "subject": subject_of(run),
            "workflow": run["workflow"] if run else "",
            "decided_by_name": person(d.get("decided_by"))["name"] if d.get("decided_by") else None,
        }

    def approval_card(a: dict[str, Any]) -> dict[str, Any]:
        run = engine.get_run(a["run_id"])
        payload = a["preview"].get("payload", {})
        name, tone, explain = present.risk(a["risk_class"])
        reasons = [r for r in a["preview"].get("reasons", []) if " is registered as " not in r]
        amount = payload.get("quote_total", payload.get("amount"))
        intent = engine.outbox.get(a["intent_id"])
        currency = payload.get("currency") or (run or {}).get("input", {}).get("lead", {}).get("currency")
        if amount is not None and not currency and run and run["input"].get("lead"):
            currency = settings.estimator["regions"].get(run["input"]["lead"].get("region", ""), {}).get("currency")
        recipients = payload.get("to") or ([payload["customer_email"]] if payload.get("customer_email") else [])
        return {
            **a,
            "payload": payload,
            "action_label": present.label_action(a["action"]),
            "risk_name": name,
            "risk_tone": tone,
            "risk_explain": explain,
            "reasons": reasons or [a["preview"].get("reasons", [""])[0]],
            "roles": [role_label(r) for r in a["required_roles"]],
            "holders": holders(a["required_roles"]),
            "subject": subject_of(run),
            "workflow": run["workflow"] if run else "",
            "title": (payload.get("lines") or [{}])[0].get("description", "").capitalize() + " invoice"
            if a["action"].startswith("billing.") and payload.get("lines")
            else a["preview"].get("summary") or present.label_action(a["action"]),
            "recipients": recipients,
            "channel": payload.get("channel"),
            "amount": present.money(amount, currency) if amount is not None else None,
            "decided_by_name": person(a["decided_by"])["name"] if a.get("decided_by") else None,
            "intent_status": intent["status"] if intent else None,
            "kind": "email"
            if a["action"] == "email.send"
            else "chat"
            if a["action"] == "chat.post"
            else "invoice"
            if a["action"].startswith("billing.")
            else "data",
        }

    def run_row(run: dict[str, Any]) -> dict[str, Any]:
        steps = engine.steps(run["id"])
        return {
            **run,
            "subject": subject_of(run),
            "context": present.run_context(run),
            "segments": [
                {
                    "tone": present.tone(s["status"]),
                    "title": f"{present.label_step(s['name'])} — {present.label_status(s['status'])}",
                }
                for s in steps
            ],
            "done": sum(s["status"] == "succeeded" for s in steps),
            "total": len(steps),
            "cost": engine.costs.run_total(run["id"]),
            "took": present.duration(run["created_at"], run["finished_at"]),
        }

    def event_view(e: dict[str, Any], intents: dict[str, Any], runs: dict[str, Any]) -> dict[str, Any] | None:
        d, ev, s = e["data"], e["event"], e["subject"]
        intent = intents.get(s or "")
        act = present.label_action(intent["action"]) if intent else (s or "")
        run = runs.get(e["run_id"] or "")
        tone, text = "neutral", None
        if ev == "run.started":
            tone, text = "info", f"Started {present.label_workflow(d.get('workflow', ''))}"
        elif ev == "agent.completed":
            via = f", handed over by {d['started_with']}" if d.get("handoffs") else ""
            tone, text = "info", f"{d['finished_with']} finished “{present.label_step(s or '')}”{via}"
        elif ev == "intent.verified":
            tone, text = "ok", f"{act} — verified at the destination"
        elif ev == "approval.requested":
            tone, text = (
                "warn",
                f"Approval requested: {present.label_action(d['action'])} ({present.risk(d['risk_class'])[0].lower()})",
            )
        elif ev in ("approval.approved", "approval.rejected"):
            verb = ev.split(".")[1]
            tone, text = ("ok" if verb == "approved" else "bad"), f"{actor_label(e['actor'])} {verb} the request"
        elif ev == "approval.denied_unauthorised":
            tone, text = "bad", f"{actor_label(e['actor'])} tried to approve without the required role"
        elif ev == "step.retry_scheduled":
            tone, text = "warn", f"{present.label_step(s or '')}: attempt {d['attempt']} failed, retrying"
        elif ev == "step.failed":
            tone, text = "bad", f"{present.label_step(s or '')} failed — {present.label_kind(d['kind']).lower()}"
        elif ev == "step.rewound":
            tone, text = "warn", f"Draft sent back by review: {d['note']}"
        elif ev == "intent.reconciled":
            found = d["found_at_destination"]
            tone, text = (
                "ok" if found else "neutral",
                f"{act}: checked the destination first — {'already there, not resent' if found else 'not there, sending'}",
            )
        elif ev == "intent.verification_failed":
            tone, text = "bad", f"{act}: the destination does not hold the write"
        elif ev == "agent.proposal_refused":
            tone, text = (
                "bad",
                f"Refused: {d['agent'].replace('_', ' ')} agent tried to {present.label_action(d['action']).lower()}",
            )
        elif ev == "exception.raised" and d.get("severity") != "info":
            tone, text = "bad", f"Exception: {present.label_kind(d['kind'])}"
        elif ev == "decision.requested":
            tone, text = "warn", "Decision requested"
        elif ev == "decision.made":
            tone, text = "ok", f"{actor_label(e['actor'])} decided: {d['choice'].replace('_', ' ')}"
        elif ev == "step.lease_expired":
            tone, text = "warn", f"{present.label_step(s or '')}: worker stopped, step resumed elsewhere"
        elif ev in ("run.duplicate_suppressed", "webhook.duplicate"):
            tone, text = "neutral", "Duplicate delivery recognised and ignored"
        elif ev == "run.succeeded":
            tone, text = "ok", "Run completed"
        elif ev == "run.failed":
            tone, text = "bad", "Run stopped with a failure"
        if text is None:
            return None
        return {
            "ts": e["ts"],
            "text": text,
            "tone": tone,
            "run_id": e["run_id"],
            "subject": subject_of(run) if run else "",
            "workflow": run["workflow"] if run else "",
        }

    def activity(run_id: str | None = None, limit: int = 14) -> list[dict[str, Any]]:
        intents = {i["id"]: i for i in engine.outbox.list(run_id=run_id)}
        runs = {r["id"]: r for r in engine.list_runs(500)}
        # A run's story reads oldest first; the global feed shows the newest first.
        entries = engine.audit.entries(run_id=run_id, limit=5000 if run_id else 600)
        items = [v for e in entries if (v := event_view(e, intents, runs))]
        return items[:limit] if limit else items

    def counts() -> dict[str, int]:
        open_exc = [x for x in engine.reporter.list("open") if x["severity"] != "info"]
        waiting = engine.conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'waiting'").fetchone()[0]
        return {
            "attention": len(engine.approvals.list("pending")) + len(pending_decisions()),
            "exceptions": len(open_exc),
            "waiting": waiting,
        }

    # ---- rendering ----------------------------------------------------------------------
    def context(request: Request, active: str) -> dict[str, Any]:
        cookie = request.cookies.get(session.COOKIE)
        who = person(session.read(secret, cookie))
        return {
            "request": request,
            "active": active,
            "user": who,
            "csrf": session.csrf_token(secret, cookie) if who else "",
            "counts": counts(),
            "env": {
                "mode": settings.mode,
                "routing": settings.pricing.get("routing", {}),
                "company": policy.company.get("name", ""),
            },
            "flash": session.unpack_flash(request.cookies.get(session.FLASH)),
            "path": request.url.path,
        }

    def render(request: Request, template: str, active: str, **data: Any) -> Response:
        with lock:
            ctx = context(request, active) | data
        response = templates.TemplateResponse(request, template, ctx)
        if ctx["flash"]:
            response.delete_cookie(session.FLASH)
        return response

    def redirect(url: str, kind: str | None = None, text: str | None = None) -> RedirectResponse:
        response = RedirectResponse(url, status_code=303)
        if kind and text:
            response.set_cookie(
                session.FLASH, session.pack_flash(kind, text), max_age=60, httponly=True, samesite="lax"
            )
        return response

    def safe_next(url: str | None, default: str = "/") -> str:
        return url if url and url.startswith("/") and not url.startswith("//") else default

    def require(request: Request, csrf: str | None, back: str) -> str | RedirectResponse:
        cookie = request.cookies.get(session.COOKIE)
        principal = session.read(secret, cookie)
        if not principal:
            return redirect(f"/login?next={quote(back)}", "info", "Sign in to make decisions.")
        if not session.csrf_ok(secret, cookie, csrf):
            raise HTTPException(403, "The form expired. Reload the page and try again.")
        return principal

    # ---- pages ----------------------------------------------------------------------------
    @router.get("/", include_in_schema=False)
    def overview(request: Request) -> Response:
        with lock:
            runs = engine.list_runs(200)
            status = Counter(r["status"] for r in runs)
            day_ago = engine.clock() - 86400
            budgets = settings.pricing.get("budgets", {})
            data = {
                "approvals": [approval_card(a) for a in engine.approvals.list("pending")][:4],
                "decisions": pending_decisions()[:3],
                "metrics": {
                    "running": status.get("running", 0),
                    "waiting": status.get("waiting", 0),
                    "completed": sum(
                        1 for r in runs if r["status"] == "succeeded" and (r["finished_at"] or 0) >= day_ago
                    ),
                    "failed": status.get("failed", 0),
                    "spend": engine.costs.day_total(),
                    "cap": float(budgets.get("per_day_usd", 0) or 0),
                },
                "recent": [run_row(r) for r in runs[:7]],
                "activity": activity(limit=12),
                "exceptions": [x for x in engine.reporter.list("open") if x["severity"] != "info"][:3],
                "chain": engine.audit.verify(),
                "greeting": present.greeting(engine.clock()),
            }
        return render(request, "overview.html", "overview", **data)

    @router.get("/approvals", include_in_schema=False)
    def approvals_page(request: Request, view: str = "pending") -> Response:
        with lock:
            pending = [approval_card(a) for a in engine.approvals.list("pending")]
            decided = [approval_card(a) for a in reversed(engine.approvals.list(None)) if a["status"] != "pending"][:40]
            decisions = pending_decisions()
        return render(
            request, "approvals.html", "approvals", view=view, pending=pending, decided=decided, decisions=decisions
        )

    @router.get("/approvals/{approval_id}", include_in_schema=False)
    def approval_page(request: Request, approval_id: str) -> Response:
        with lock:
            raw = engine.approvals.get(approval_id)
            if raw is None:
                raise HTTPException(404, "No such approval")
            card = approval_card(raw)
        return render(request, "approval.html", "approvals", a=card)

    @router.get("/runs", include_in_schema=False)
    def runs_page(request: Request, status: str = "all") -> Response:
        with lock:
            runs = engine.list_runs(300)
            tally = Counter(r["status"] for r in runs)
            shown = [run_row(r) for r in runs if status == "all" or r["status"] == status]
        return render(request, "runs.html", "runs", runs=shown, status=status, tally=tally, total=len(runs))

    @router.get("/runs/{run_id}", include_in_schema=False)
    def run_page(request: Request, run_id: str) -> Response:
        with lock:
            run = engine.get_run(run_id)
            if run is None:
                raise HTTPException(404, "No such run")
            view = engine.run_view(run_id)
            definition = engine.workflows[run["workflow"]]
            agents: dict[str, list[dict[str, Any]]] = {}
            for e in engine.audit.entries(run_id=run_id, limit=5000):
                if e["event"] == "agent.completed":
                    agents.setdefault(e["subject"], []).append(e["data"])
            steps = []
            for s in view["steps"]:
                waiting = s["waiting_on"] or ""
                kind, _, ref = waiting.partition(":")
                steps.append(
                    {
                        **s,
                        "title": present.label_step(s["name"]),
                        "description": definition.step(s["name"]).description,
                        "duration": present.duration(s["started_at"], s["finished_at"]),
                        "agents": agents.get(s["name"], []),
                        "waiting_kind": kind,
                        "waiting_ref": ref,
                        "facts": _facts(s["output"], s["name"]),
                    }
                )
            outputs = {s["name"]: s["output"] for s in view["steps"] if s["status"] == "succeeded" and s["output"]}
            parent = engine.get_run(run["parent_run_id"]) if run["parent_run_id"] else None
            data = {
                "run": run_row(run),
                "steps": steps,
                "intents": [
                    {**i, "label": present.label_action(i["action"]), "risk_name": present.risk(i["risk_class"])[0]}
                    for i in view["intents"]
                    if i["step"] != "_notify"
                ],
                "notices": [i for i in view["intents"] if i["step"] == "_notify"],
                "approvals": [approval_card(a) for a in view["approvals"]],
                "decisions": [decision_card(dict(d)) for d in view["decisions"]],
                "exceptions": view["exceptions"],
                "costs": view["costs"],
                "cost_total": view["cost_total_usd"],
                "children": [run_row(c) for c in engine.child_runs(run_id)],
                "parent": run_row(parent) if parent else None,
                "lead": run["input"].get("lead"),
                "case": run["input"].get("case"),
                "message": run["input"].get("message"),
                "survey": run["input"].get("survey"),
                "estimate": outputs.get("estimate"),
                "draft": (outputs.get("draft_proposal") or {}).get("draft"),
                "review": outputs.get("review"),
                "activity": activity(run_id, limit=0),
            }
        return render(request, "run.html", "runs", **data)

    @router.get("/exceptions", include_in_schema=False)
    def exceptions_page(request: Request, view: str = "open") -> Response:
        with lock:
            rows = engine.reporter.list("open" if view == "open" else "resolved")
            if view == "open":
                rows = [x for x in rows if x["severity"] != "info"]
            items = []
            for x in reversed(rows):
                run = engine.get_run(x["run_id"]) if x["run_id"] else None
                step = next((s for s in engine.steps(x["run_id"]) if s["name"] == x["step"]), None) if run else None
                decision = None
                if x["kind"] == "needs_decision" and x["details"].get("decision_id"):
                    row = engine.conn.execute(
                        "SELECT * FROM decisions WHERE id = ?", (x["details"]["decision_id"],)
                    ).fetchone()
                    decision = decision_card(dict(row)) if row and row["status"] == "pending" else None
                items.append(
                    {
                        **x,
                        "subject": subject_of(run),
                        "workflow": run["workflow"] if run else "",
                        "decision": decision,
                        "can_retry": bool(
                            step and step["status"] in ("failed", "blocked") and x["kind"] in present.RETRYABLE_KINDS
                        ),
                    }
                )
            info = len([x for x in engine.reporter.list("open") if x["severity"] == "info"])
        return render(request, "exceptions.html", "exceptions", items=items, view=view, info_count=info)

    @router.get("/audit", include_in_schema=False)
    def audit_page(request: Request, run: str | None = None) -> Response:
        with lock:
            chain = engine.audit.verify()
            entries = engine.audit.entries(run_id=run, limit=400)
            if run:
                entries = list(reversed(entries))
            intents = {i["id"]: i for i in engine.outbox.list()}
            runs = {r["id"]: r for r in engine.list_runs(500)}
            rows = []
            for e in entries[:300]:
                view = event_view(e, intents, runs)
                rows.append(
                    {
                        **e,
                        "actor_label": actor_label(e["actor"]),
                        "text": view["text"]
                        if view
                        else AUDIT_TEXT.get(
                            e["event"], e["event"].replace(".", " ").replace("_", " ").capitalize()
                        ).format(
                            action=present.label_action(intents[e["subject"]]["action"]).lower()
                            if e["subject"] in intents
                            else e["data"].get("action", "action"),
                            step=present.label_step(e["subject"] or "").lower(),
                        ),
                        "tone": view["tone"] if view else "neutral",
                        "data_json": json.dumps(e["data"], indent=2, ensure_ascii=False),
                    }
                )
            head = engine.conn.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        return render(
            request, "audit.html", "audit", chain=chain, rows=rows, run_filter=run, head=dict(head) if head else None
        )

    @router.get("/costs", include_in_schema=False)
    def costs_page(request: Request) -> Response:
        with lock:
            budgets = settings.pricing.get("budgets", {})
            by_run = engine.conn.execute(
                "SELECT run_id, SUM(cost_usd) AS cost, SUM(requests) AS requests, MAX(estimated) AS estimated"
                " FROM usage GROUP BY run_id ORDER BY cost DESC LIMIT 12"
            ).fetchall()
            top = []
            for r in by_run:
                run = engine.get_run(r["run_id"])
                if run:
                    top.append(
                        {"run": run_row(run), "cost": r["cost"], "requests": r["requests"], "estimated": r["estimated"]}
                    )
            totals = engine.conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS cost, COALESCE(SUM(input_tokens),0) AS input, COALESCE(SUM(output_tokens),0) AS output,"
                " COALESCE(SUM(requests),0) AS requests, MAX(estimated) AS estimated FROM usage"
            ).fetchone()
            data = {
                "today": engine.costs.day_total(),
                "cap_day": float(budgets.get("per_day_usd", 0) or 0),
                "cap_run": float(budgets.get("per_run_usd", 0) or 0),
                "breakdown": engine.costs.breakdown(),
                "top": top,
                "totals": dict(totals),
                "prices": settings.pricing.get("models", {}),
            }
        return render(request, "costs.html", "costs", **data)

    @router.get("/login", include_in_schema=False)
    def login_page(request: Request, next: str = "/") -> Response:
        demo = []
        if demo_sign_in:
            for pid in policy.principals:
                p = person(pid)
                p["approves"] = [  # type: ignore[index]
                    present.risk(cls)[0].lower()
                    for cls, spec in policy.classes.items()
                    if set(spec.get("approver_roles", [])) & set(p["roles"])  # type: ignore[index]
                ]
                demo.append(p)
        return render(request, "login.html", "login", next=safe_next(next), demo=demo)

    # ---- actions ------------------------------------------------------------------------
    def sign_in(request: Request, principal: str, next_url: str) -> RedirectResponse:
        response = redirect(safe_next(next_url), "ok", f"Signed in as {person(principal)['name']}.")  # type: ignore[index]
        response.set_cookie(
            session.COOKIE,
            session.issue(secret, principal),
            max_age=session.TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    @router.post("/login", include_in_schema=False)
    def login(request: Request, token: str = Form(""), next: str = Form("/")) -> Response:
        principal = settings.principal_for_token(token.strip())
        if not principal:
            return redirect(f"/login?next={quote(safe_next(next))}", "bad", "That token is not valid.")
        with lock:
            engine.audit.record("console.signed_in", actor=principal, data={"method": "token"})
        return sign_in(request, principal, next)

    @router.post("/login/demo", include_in_schema=False)
    def login_demo(request: Request, principal: str = Form(...), next: str = Form("/")) -> Response:
        if not demo_sign_in or principal not in policy.principals:
            raise HTTPException(404)
        with lock:
            engine.audit.record("console.signed_in", actor=principal, data={"method": "demo"})
        return sign_in(request, principal, next)

    @router.post("/logout", include_in_schema=False)
    def logout() -> Response:
        response = redirect("/", "ok", "Signed out.")
        response.delete_cookie(session.COOKIE)
        return response

    @router.post("/approvals/{approval_id}/decide", include_in_schema=False)
    def decide(
        request: Request, approval_id: str, decision: str = Form(...), note: str = Form(""), csrf: str = Form("")
    ) -> Response:
        back = f"/approvals/{approval_id}"
        who = require(request, csrf, back)
        if isinstance(who, RedirectResponse):
            return who
        with lock:
            try:
                engine.decide_approval(approval_id, who, decision, note.strip() or None)
            except ApprovalError as exc:
                return redirect(back, "bad", str(exc).capitalize())
        verb = (
            "Approved — it will be sent and verified by the worker."
            if decision == "approve"
            else "Rejected — nothing will be sent."
        )
        return redirect(back, "ok", verb)

    @router.post("/decisions/{decision_id}/answer", include_in_schema=False)
    def answer(
        request: Request, decision_id: str, choice: str = Form(...), csrf: str = Form(""), next: str = Form("/")
    ) -> Response:
        back = safe_next(next)
        who = require(request, csrf, back)
        if isinstance(who, RedirectResponse):
            return who
        with lock:
            try:
                engine.answer_decision(decision_id, who, choice)
            except (ValueError, PermissionError) as exc:
                return redirect(back, "bad", str(exc))
        return redirect(back, "ok", f"Decision recorded: {choice.replace('_', ' ')}. The workflow continues.")

    @router.post("/runs/{run_id}/steps/{step}/retry", include_in_schema=False)
    def retry(request: Request, run_id: str, step: str, csrf: str = Form(""), next: str = Form("")) -> Response:
        back = safe_next(next, f"/runs/{run_id}")
        who = require(request, csrf, back)
        if isinstance(who, RedirectResponse):
            return who
        with lock:
            engine.retry_step(run_id, step, who)
        return redirect(
            back,
            "ok",
            f"{present.label_step(step)}: retry scheduled. Earlier writes are reconciled first, never resent blindly.",
        )

    @router.post("/exceptions/{exception_id}/resolve", include_in_schema=False)
    def resolve(request: Request, exception_id: str, csrf: str = Form(""), next: str = Form("/exceptions")) -> Response:
        back = safe_next(next, "/exceptions")
        who = require(request, csrf, back)
        if isinstance(who, RedirectResponse):
            return who
        with lock:
            engine.reporter.resolve(exception_id, who, "resolved in the console")
        return redirect(back, "ok", "Marked as resolved.")

    return router


# Plain wording for audit events the activity feed leaves out.
AUDIT_TEXT = {
    "intent.proposed": "Action proposed: {action}",
    "intent.gate_passed": "Policy check passed: {action}",
    "intent.executing": "Calling the destination: {action}",
    "intent.executed": "Destination accepted the write: {action}",
    "intent.revised": "Content changed: {action}",
    "intent.transient_error": "Temporary failure, will retry: {action}",
    "intent.outcome_unknown": "No reply after sending: {action}",
    "intent.failed": "Failed: {action}",
    "intent.reset": "Reset for another attempt: {action}",
    "intent.rejected": "Not sent: {action}",
    "intent.refused": "Refused by policy: {action}",
    "intent.revision_ignored": "Later change ignored, already sent: {action}",
    "step.claimed": "Step started: {step}",
    "step.succeeded": "Step finished: {step}",
    "step.waiting": "Step paused for a person: {step}",
    "step.woken": "Step resumed: {step}",
    "step.retry_requested": "Retry requested: {step}",
    "step.lease_lost": "Stale worker stopped: {step}",
    "step.result_discarded": "Stale result discarded: {step}",
    "run.waiting": "Run waiting for a person",
    "run.running": "Run moving again",
    "cost.recorded": "Model usage recorded",
    "console.signed_in": "Signed in to the console",
    "agent.proposal_received": "Agent proposed an action",
    "approval.expired": "Approval request expired",
    "approval.not_usable": "Approval could not be used",
    "exception.raised": "Exception recorded",
    "exception.resolved": "Exception resolved",
    "webhook.rejected": "Webhook with a bad signature rejected",
}

_HIDDEN_FACTS = {
    "draft",
    "checks",
    "reviewer",
    "approved_draft_hash",
    "inputs",
    "assumptions",
    "refused_proposals",
    "event_id",
}

# The facts worth a glance for each step; other steps show their first few short values.
STEP_FACTS = {
    "estimate": [
        ("system_kwp", "System", " kWp"),
        ("panel_count", "Panels", ""),
        ("payback_years", "Payback", " years"),
    ],
    "draft_proposal": [("revision", "Revision", "")],
    "schedule_survey": [("start", "Slot", ""), ("reason", "Why", "")],
    "route": [("handled_by", "Handled by", ""), ("qualification", "Qualified as", ""), ("urgency", "Urgency", "")],
    "send_proposal": [("approved_by", "Approved by", "")],
}


def _facts(output: dict[str, Any] | None, step: str = "") -> list[tuple[str, str]]:
    """A few readable key facts from a step's output."""
    if not output:
        return []
    if step in STEP_FACTS:
        return [
            (label, f"{output[key]}{unit}")
            for key, label, unit in STEP_FACTS[step]
            if output.get(key) not in (None, "", 0)
        ]
    facts = []
    for key, value in output.items():
        if key in _HIDDEN_FACTS or isinstance(value, (dict, list)) or value is None:
            continue
        text = str(value)
        if len(text) > 64:
            continue
        facts.append((key.replace("_", " ").capitalize(), text))
    return facts[:4]
