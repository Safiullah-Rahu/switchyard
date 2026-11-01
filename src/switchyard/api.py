"""HTTP surface: signed webhooks in, approvals and decisions from people, a console to look at.

One process can serve the API and run the worker (`switchyard serve`), which is
the smallest deployment that works. The worker uses its own database
connection; the API's connection is serialised with a lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import threading
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import __version__
from .approvals import ApprovalError
from .canonical import payload_hash
from .config import Settings, load_settings
from .console import render_approval, render_console
from .db import tx
from .engine import Engine

REQUIRED_MESSAGE_FIELDS = ("from_name", "from_email", "subject", "body")


def sign(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def create_app(settings: Settings | None = None, *, start_worker: bool = False, poll_seconds: float = 1.0) -> FastAPI:
    settings = settings or load_settings()
    engine = Engine(settings, worker_id="api")
    lock = threading.RLock()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = None
        worker: Engine | None = None
        if start_worker:
            worker = Engine(settings)

            async def loop() -> None:
                while True:
                    try:
                        await worker.tick()
                    except Exception as exc:  # keep the worker alive; the error is in the audit/exception log
                        worker.reporter.raise_exception(
                            kind="agent_error",
                            severity="high",
                            message=f"worker tick failed: {exc}",
                            dedup=f"worker|{type(exc).__name__}",
                        )
                    await asyncio.sleep(poll_seconds)

            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if worker:
            await worker.aclose()

    app = FastAPI(title="Switchyard", version=__version__, lifespan=lifespan)
    app.state.engine = engine

    def principal(authorization: str | None) -> str:
        token = (authorization or "").removeprefix("Bearer ").strip()
        who = settings.principal_for_token(token) if token else None
        if not who:
            raise HTTPException(401, "a valid approver token is required")
        return who

    async def verified_json(request: Request, signature: str | None) -> tuple[bytes, dict[str, Any]]:
        raw = await request.body()
        if not signature or not hmac.compare_digest(sign(settings.webhook_secret, raw), signature):
            with lock:
                engine.audit.record(
                    "webhook.rejected", actor="api", data={"path": request.url.path, "reason": "bad signature"}
                )
            raise HTTPException(401, "invalid signature")
        try:
            return raw, json.loads(raw)
        except ValueError:
            raise HTTPException(400, "body is not JSON") from None

    def record_event(source: str, event_id: str, raw: bytes) -> dict[str, Any] | None:
        """Returns the earlier delivery if this event id was already received."""
        with tx(engine.conn):
            prior = engine.conn.execute(
                "SELECT * FROM inbound_events WHERE source = ? AND event_id = ?", (source, event_id)
            ).fetchone()
            if prior:
                engine.audit.record("webhook.duplicate", actor=source, run_id=prior["run_id"], subject=event_id)
                return dict(prior)
            engine.conn.execute(
                "INSERT INTO inbound_events (source, event_id, payload_hash, received_at) VALUES (?,?,?,?)",
                (source, event_id, payload_hash(raw.decode("utf-8", "replace")), engine.clock()),
            )
            return None

    def attach_run(source: str, event_id: str, run_id: str) -> None:
        engine.conn.execute(
            "UPDATE inbound_events SET run_id = ? WHERE source = ? AND event_id = ?", (run_id, source, event_id)
        )

    # ---- webhooks ----------------------------------------------------------------
    @app.post("/webhooks/inbound", status_code=202)
    async def inbound(
        request: Request,
        x_switchyard_signature: str | None = Header(None),
        x_switchyard_event_id: str | None = Header(None),
    ) -> dict[str, Any]:
        raw, message = await verified_json(request, x_switchyard_signature)
        missing = [f for f in REQUIRED_MESSAGE_FIELDS if not message.get(f)]
        if missing:
            raise HTTPException(422, f"missing fields: {missing}")
        source = message.get("source", "web_form")
        event_id = x_switchyard_event_id or message.get("event_id") or payload_hash(message)[:16]
        with lock:
            prior = record_event(source, event_id, raw)
            if prior:
                return {"duplicate": True, "reason": "event id already received", "run_id": prior["run_id"]}
            run_id, created = engine.start_run("inbound_triage", {"message": message}, actor=f"webhook:{source}")
            attach_run(source, event_id, run_id)
        return {
            "duplicate": not created,
            "reason": None if created else "same message content already received",
            "run_id": run_id,
        }

    @app.post("/webhooks/survey-completed", status_code=202)
    async def survey_completed(
        request: Request,
        x_switchyard_signature: str | None = Header(None),
        x_switchyard_event_id: str | None = Header(None),
    ) -> dict[str, Any]:
        raw, body = await verified_json(request, x_switchyard_signature)
        event_id = x_switchyard_event_id or body.get("event_id") or payload_hash(body)[:16]
        with lock:
            prior = record_event("field_app", event_id, raw)
            if prior:
                return {"duplicate": True, "run_id": prior["run_id"]}
            run_id, created = engine.start_run("survey_to_install", body, actor="webhook:field_app")
            attach_run("field_app", event_id, run_id)
        return {"duplicate": not created, "run_id": run_id}

    # ---- people ------------------------------------------------------------------------
    @app.get("/approvals")
    def approvals(status: str | None = "pending") -> list[dict[str, Any]]:
        with lock:
            return engine.approvals.list(status=status or None)

    @app.post("/approvals/{approval_id}/decision")
    def decide(
        approval_id: str, payload: dict[str, Any] = Body(...), authorization: str | None = Header(None)
    ) -> dict[str, Any]:
        who = principal(authorization)
        with lock:
            try:
                return engine.decide_approval(approval_id, who, payload.get("decision", ""), payload.get("note"))
            except ApprovalError as exc:
                raise HTTPException(403, str(exc)) from None

    @app.post("/decisions/{decision_id}")
    def answer(
        decision_id: str, payload: dict[str, Any] = Body(...), authorization: str | None = Header(None)
    ) -> dict[str, str]:
        who = principal(authorization)
        with lock:
            try:
                engine.answer_decision(decision_id, who, payload.get("choice", ""))
            except (ValueError, PermissionError) as exc:
                raise HTTPException(400, str(exc)) from None
        return {"status": "decided", "by": who}

    @app.post("/runs/{run_id}/steps/{step}/retry")
    def retry(run_id: str, step: str, authorization: str | None = Header(None)) -> dict[str, str]:
        who = principal(authorization)
        with lock:
            engine.retry_step(run_id, step, who)
        return {"status": "retry scheduled", "by": who}

    # ---- reading -----------------------------------------------------------------------
    @app.get("/runs")
    def runs(limit: int = 50) -> list[dict[str, Any]]:
        with lock:
            return engine.list_runs(limit)

    @app.get("/runs/{run_id}")
    def run(run_id: str) -> dict[str, Any]:
        with lock:
            if engine.get_run(run_id) is None:
                raise HTTPException(404, "no such run")
            return engine.run_view(run_id)

    @app.get("/exceptions")
    def exceptions(status: str | None = "open") -> list[dict[str, Any]]:
        with lock:
            return engine.reporter.list(status=status or None)

    @app.get("/costs")
    def costs() -> dict[str, Any]:
        with lock:
            return {
                "today_usd": engine.costs.day_total(),
                "by_agent": engine.costs.breakdown(),
                "budgets": settings.pricing.get("budgets", {}),
            }

    @app.get("/audit/verify")
    def audit_verify() -> dict[str, Any]:
        with lock:
            check = engine.audit.verify()
        return check.__dict__

    @app.get("/health")
    def health() -> dict[str, Any]:
        with lock:
            pending = len(engine.approvals.list("pending"))
            open_exc = len(engine.reporter.list("open"))
        return {
            "ok": True,
            "version": __version__,
            "mode": settings.mode,
            "pending_approvals": pending,
            "open_exceptions": open_exc,
        }

    @app.get("/", response_class=HTMLResponse)
    def console() -> str:
        with lock:
            return render_console(engine)

    @app.get("/approvals/{approval_id}/review", response_class=HTMLResponse)
    def review(approval_id: str) -> str:
        with lock:
            approval = engine.approvals.get(approval_id)
            if approval is None:
                raise HTTPException(404, "no such approval")
            return render_approval(engine, approval)

    return app
