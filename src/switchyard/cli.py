"""Command line: run the worker and API, act as an operator, inspect state, run the demos."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .config import load_settings
from .engine import Engine

console = Console()


def _utf8_output() -> None:
    """Write UTF-8 even where Windows hands the process a legacy code page (cp1252 when output is piped or
    redirected). The commands print arrows and dashes, and an encoding error would only surface after a command had
    already acted (a fault armed, an approval recorded), exiting 1 as if it had failed."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").replace("-", "").lower()
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _engine(args: argparse.Namespace, **overrides: Any) -> Engine:
    if getattr(args, "lease_seconds", None):
        overrides["lease_seconds"] = args.lease_seconds
    return Engine(load_settings(args.home, mode=args.mode, **overrides), worker_id=getattr(args, "worker_id", None))


def _table(title: str, columns: list[str], rows: list[list[Any]]) -> None:
    t = Table(title=title, title_justify="left", header_style="bold")
    for c in columns:
        t.add_column(c, overflow="fold")
    for r in rows:
        t.add_row(*[str(x) if x is not None else "" for x in r])
    console.print(t)


# ---- commands ------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> None:
    settings = load_settings(args.home, mode=args.mode)
    Engine(settings).close()
    console.print(f"State directory: [bold]{settings.home}[/] (mode: {settings.mode})")
    console.print(f"Secrets file:    {settings.home / 'secrets.json'} — keep it out of version control")
    _table(
        "Approver tokens (demo only — use SSO in production)",
        ["principal", "roles", "token"],
        [
            [who, ", ".join(settings.policy["principals"][who]["roles"]), tok]
            for tok, who in settings.approver_tokens.items()
        ],
    )


def cmd_worker(args: argparse.Namespace) -> None:
    async def run() -> None:
        engine = _engine(args)
        console.print(f"worker {engine.worker_id} started (mode {engine.settings.mode})")
        try:
            if args.until_idle:
                await engine.run_until_idle(timeout=args.timeout, wait_for_leases=True)
            else:
                while True:
                    await engine.tick()
                    await asyncio.sleep(args.poll)
        finally:
            await engine.aclose()

    asyncio.run(run())


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app

    app = create_app(load_settings(args.home, mode=args.mode), start_worker=not args.no_worker, poll_seconds=args.poll)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def cmd_submit(args: argparse.Namespace) -> None:
    from .demo.fixtures import MESSAGES, message

    if args.message in MESSAGES:
        m = message(args.message, args.event_id)
    else:
        m = json.loads(Path(args.message).read_text("utf-8"))
    engine = _engine(args)
    run_id, created = engine.start_run("inbound_triage", {"message": m, "demo_flags": args.flag or []}, actor="cli")
    console.print(f"{'started' if created else 'already received (duplicate suppressed)'}: [bold]{run_id}[/]")


def cmd_runs(args: argparse.Namespace) -> None:
    engine = _engine(args)
    rows = []
    for r in engine.list_runs(args.limit):
        steps = " ".join(f"{s['name']}:{s['status']}" for s in engine.steps(r["id"]))
        rows.append([r["id"], r["workflow"], r["status"], f"${engine.costs.run_total(r['id']):.4f}", steps])
    _table("Runs", ["run", "workflow", "status", "cost", "steps"], rows)


def cmd_show(args: argparse.Namespace) -> None:
    engine = _engine(args)
    view = engine.run_view(args.run_id)
    run = view["run"]
    console.print(f"[bold]{run['id']}[/] {run['workflow']} v{run['version']} — {run['status']}")
    _table(
        "Steps",
        ["step", "status", "attempts", "waiting on", "last error"],
        [
            [s["name"], s["status"], s["attempts"], s["waiting_on"], (s["last_error"] or "")[:120]]
            for s in view["steps"]
        ],
    )
    _table(
        "Side effects (intents)",
        ["slot", "action", "class", "status", "destination id"],
        [
            [i["slot"], i["action"], i["risk_class"], i["status"], (i["receipt"] or {}).get("external_id")]
            for i in view["intents"]
        ],
    )
    if view["approvals"]:
        _table(
            "Approvals",
            ["id", "action", "status", "decided by"],
            [[a["id"], a["action"], a["status"], a["decided_by"]] for a in view["approvals"]],
        )
    if view["exceptions"]:
        _table(
            "Exceptions",
            ["kind", "severity", "status", "message"],
            [[x["kind"], x["severity"], x["status"], x["message"][:120]] for x in view["exceptions"]],
        )
    console.print(f"model cost: ${view['cost_total_usd']:.4f}")


def cmd_approvals(args: argparse.Namespace) -> None:
    engine = _engine(args)
    _table(
        "Pending approvals",
        ["id", "action", "class", "who can approve", "summary"],
        [
            [a["id"], a["action"], a["risk_class"], ", ".join(a["required_roles"]), a["preview"].get("summary")]
            for a in engine.approvals.list("pending")
        ],
    )


def cmd_decide_approval(args: argparse.Namespace, decision: str) -> None:
    engine = _engine(args)
    a = engine.decide_approval(args.approval_id, args.as_, decision, args.note)
    console.print(f"{a['id']}: {a['status']} by {a['decided_by']}")


def cmd_decide(args: argparse.Namespace) -> None:
    engine = _engine(args)
    engine.answer_decision(args.decision_id, args.as_, args.choice)
    console.print(f"{args.decision_id}: {args.choice}")


def cmd_retry(args: argparse.Namespace) -> None:
    engine = _engine(args)
    engine.retry_step(args.run_id, args.step, args.as_)
    console.print(f"{args.run_id}/{args.step}: retry scheduled")


def cmd_exceptions(args: argparse.Namespace) -> None:
    engine = _engine(args)
    _table(
        "Open exceptions",
        ["id", "severity", "kind", "run", "message", "next step"],
        [
            [x["id"], x["severity"], x["kind"], x["run_id"], x["message"][:100], x.get("suggested_action")]
            for x in engine.reporter.list("open")
        ],
    )


def cmd_digest(args: argparse.Namespace) -> None:
    engine = _engine(args)
    text = engine.reporter.digest()
    print(text)
    if args.post:  # e.g. from cron at 08:00; once per day, through the outbox like any other post
        day = time.strftime("%Y-%m-%d", time.gmtime(engine.clock()))
        intent = engine.outbox.propose(
            run_id="_system",
            step="_digest",
            slot=f"digest:{day}",
            action="chat.post",
            payload={"channel": args.post, "text": text},
            proposed_by="cli",
            origin="system",
        )
        console.print(f"posted to {args.post}: {engine.outbox.process(intent['id'])['status']}")


def cmd_costs(args: argparse.Namespace) -> None:
    engine = _engine(args)
    _table(
        f"Model cost today: ${engine.costs.day_total():.4f}",
        ["agent", "model", "requests", "input tok", "output tok", "USD", "estimated"],
        [
            [
                r["agent"],
                r["model"],
                r["requests"],
                r["input_tokens"],
                r["output_tokens"],
                f"{r['cost_usd']:.5f}",
                "yes" if r["estimated"] else "no",
            ]
            for r in engine.costs.breakdown()
        ],
    )


def cmd_audit_verify(args: argparse.Namespace) -> None:
    check = _engine(args).audit.verify()
    if check.ok:
        console.print(f"[green]audit chain intact[/] — {check.checked} entries")
    else:
        console.print(f"[red]audit chain broken at entry {check.first_bad_seq}[/]: {check.reason}")
        sys.exit(1)


def cmd_sandbox(args: argparse.Namespace) -> None:
    engine = _engine(args)
    apps = engine.reads
    if args.what == "counts":
        _table("Simulated apps", ["table", "rows"], [[k, v] for k, v in apps.counts().items()])
    elif args.what == "emails":
        _table(
            "Sent emails",
            ["to", "subject", "message id"],
            [[", ".join(e["to"]), e["subject"], e["message_id"]] for e in apps.email_search()],
        )
    elif args.what == "chat":
        _table("Chat", ["channel", "text"], [[m["channel"], m["text"]] for m in apps.chat_history()])
    elif args.what == "calls":
        _table(
            "API calls seen by the simulated apps",
            ["app", "action", "outcome"],
            [[c["app"], c["action"], c["outcome"]] for c in apps.call_log()],
        )


def cmd_fault(args: argparse.Namespace) -> None:
    engine = _engine(args)
    if args.clear:
        engine.reads.clear_faults()
        console.print("faults cleared")
        return
    engine.reads.set_fault(args.app, args.action, args.mode_, args.times)
    console.print(f"armed: {args.app}.{args.action} → {args.mode_} ×{args.times}")


def cmd_demo(args: argparse.Namespace) -> None:
    from .demo.scenarios import SCENARIOS, render_markdown, run_scenarios

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = asyncio.run(run_scenarios(names, console=console))
    if args.report:
        Path(args.report).write_text(render_markdown(results), "utf-8")
        console.print(f"report written to {args.report}")
    if not all(r.passed for r in results):
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    _utf8_output()
    parser = argparse.ArgumentParser(prog="switchyard", description="Durable, policy-gated agent workflows.")
    parser.add_argument("--home", help="state directory (default .switchyard or $SWITCHYARD_HOME)")
    parser.add_argument("--mode", choices=["offline", "live"], help="offline playbooks or live OpenAI models")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create state, print approver tokens").set_defaults(fn=cmd_init)

    p = sub.add_parser("worker", help="run the scheduler loop")
    p.add_argument("--until-idle", action="store_true")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument("--lease-seconds", type=float)
    p.add_argument("--worker-id", help="stable name for this worker in the audit log")
    p.set_defaults(fn=cmd_worker)

    p = sub.add_parser("serve", help="API + console + worker in one process")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument("--no-worker", action="store_true")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("submit", help="start inbound triage for a sample message or a JSON file")
    p.add_argument("message", help="bakery | injection | service | existing_customer_lead | path.json")
    p.add_argument("--event-id")
    p.add_argument("--flag", action="append", help="demo flag, e.g. compromised_model")
    p.set_defaults(fn=cmd_submit)

    p = sub.add_parser("runs")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_runs)
    p = sub.add_parser("show")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_show)

    sub.add_parser("approvals").set_defaults(fn=cmd_approvals)
    for name, decision in (("approve", "approve"), ("reject", "reject")):
        p = sub.add_parser(name)
        p.add_argument("approval_id")
        p.add_argument("--as", dest="as_", required=True, help="principal id, e.g. maya.patel")
        p.add_argument("--note")
        p.set_defaults(fn=lambda a, d=decision: cmd_decide_approval(a, d))

    p = sub.add_parser("decide")
    p.add_argument("decision_id")
    p.add_argument("choice")
    p.add_argument("--as", dest="as_", required=True)
    p.set_defaults(fn=cmd_decide)

    p = sub.add_parser("retry", help="retry a failed step after fixing its cause")
    p.add_argument("run_id")
    p.add_argument("step")
    p.add_argument("--as", dest="as_", required=True)
    p.set_defaults(fn=cmd_retry)

    sub.add_parser("exceptions").set_defaults(fn=cmd_exceptions)
    p = sub.add_parser("digest", help="summary of runs, approvals, spend and exceptions")
    p.add_argument("--post", metavar="CHANNEL", help="also post it to a team channel, once per day")
    p.set_defaults(fn=cmd_digest)
    sub.add_parser("costs").set_defaults(fn=cmd_costs)
    sub.add_parser("audit-verify").set_defaults(fn=cmd_audit_verify)

    p = sub.add_parser("sandbox", help="inspect the simulated business apps")
    p.add_argument("what", choices=["counts", "emails", "chat", "calls"])
    p.set_defaults(fn=cmd_sandbox)

    p = sub.add_parser("fault", help="arm a failure in a simulated app")
    p.add_argument("app", nargs="?")
    p.add_argument("action", nargs="?")
    p.add_argument(
        "mode_",
        nargs="?",
        metavar="mode",
        choices=["transient", "permanent", "timeout_after_write", "drop", "crash_after_write"],
    )
    p.add_argument("--times", type=int, default=1)
    p.add_argument("--clear", action="store_true")
    p.set_defaults(fn=cmd_fault)

    p = sub.add_parser("demo", help="run the failure-mode scenarios")
    p.add_argument("scenario", nargs="?", default="all")
    p.add_argument("--report", help="write a Markdown report here")
    p.set_defaults(fn=cmd_demo)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
