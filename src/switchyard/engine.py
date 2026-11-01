"""Durable workflow engine.

A workflow is a small DAG of named steps. Every step's state lives in the
database, so any worker can pick up any run after a restart:

    pending ─claim─► running ─┬─► succeeded
                              ├─► waiting      (approval / decision; woken by a person)
                              ├─► retry_wait   (backoff, then pending again)
                              └─► failed ──► dependents become blocked

A claim takes a *lease* with a fencing token. If a worker dies, the lease
expires and another worker resumes the step; if the first worker comes back,
its token no longer matches and its writes are refused (LeaseLost).

Steps never touch the outside world directly: they call `ctx.act(...)`, which
goes through the outbox (policy → approval → execute → verify).
"""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import db
from .adapters.base import AdapterRegistry, SimulatedCrash
from .approvals import Approvals
from .audit import AuditLog
from .canonical import dumps, loads, new_id
from .config import Settings
from .costs import BudgetExceeded, CostMeter
from .db import tx
from .outbox import Fence, IntentFailed, IntentRetry, IntentWaiting, LeaseLost, Outbox
from .policy import Policy, PolicyViolation
from .reporting import Reporter

# ---- step results and signals -------------------------------------------------


@dataclass
class Done:
    output: dict[str, Any] = field(default_factory=dict)


@dataclass
class Rewind:
    """Send an earlier step back for another attempt with a note (e.g. reviewer findings)."""

    target: str
    note: str
    details: dict[str, Any] = field(default_factory=dict)


class StepRetry(Exception):
    def __init__(self, message: str, delay: float | None = None, kind: str = "step_error"):
        super().__init__(message)
        self.delay = delay
        self.kind = kind


class StepFatal(Exception):
    def __init__(
        self, message: str, kind: str = "step_failed", severity: str = "high", details: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.kind = kind
        self.severity = severity
        self.details = details or {}


class WaitingApproval(Exception):
    def __init__(self, approval: dict[str, Any]):
        super().__init__(f"waiting for approval {approval['id']}")
        self.approval = approval


class WaitingDecision(Exception):
    def __init__(self, decision_id: str, question: str):
        super().__init__(f"waiting for decision {decision_id}: {question}")
        self.decision_id = decision_id


StepHandler = Callable[["StepContext"], Awaitable[Done | Rewind]]


@dataclass
class StepDef:
    name: str
    handler: StepHandler
    depends_on: tuple[str, ...] = ()
    max_attempts: int = 6
    description: str = ""


@dataclass
class WorkflowDef:
    name: str
    version: int
    steps: list[StepDef]
    description: str = ""
    dedup: Callable[[dict[str, Any]], str | None] | None = None

    def __post_init__(self) -> None:
        names = [s.name for s in self.steps]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.name}: duplicate step names")
        seen: set[str] = set()
        for s in self.steps:  # steps must be listed in dependency order → no cycles
            missing = [d for d in s.depends_on if d not in seen]
            if missing:
                raise ValueError(f"{self.name}.{s.name} depends on {missing} which are not listed before it")
            seen.add(s.name)

    def step(self, name: str) -> StepDef:
        return next(s for s in self.steps if s.name == name)

    def dependents(self, name: str) -> set[str]:
        out: set[str] = set()
        frontier = {name}
        while frontier:
            nxt = {s.name for s in self.steps if set(s.depends_on) & frontier} - out
            out |= nxt
            frontier = nxt
        return out


# ---- step context -----------------------------------------------------------------


class StepContext:
    def __init__(self, engine: Engine, run: dict[str, Any], step: dict[str, Any], fence: Fence):
        self.engine = engine
        self.run_id: str = run["id"]
        self.workflow: str = run["workflow"]
        self.input: dict[str, Any] = run["input"]
        self.step: str = step["name"]
        self.attempt: int = step["attempts"]
        self.notes: list[dict[str, Any]] = step["notes"]
        self.fence = fence
        self.proposals: list[dict[str, Any]] = []
        self.settings = engine.settings

    @property
    def reads(self) -> Any:
        """Read-only view of the business apps (CRM search, calendar availability …)."""
        return self.engine.reads

    def output_of(self, step: str) -> dict[str, Any]:
        return self.engine.step_output(self.run_id, step)

    def act(self, action: str, payload: dict[str, Any], slot: str = "main") -> dict[str, Any]:
        """Perform one side effect exactly once. Returns the verified intent (with receipt)."""
        outbox = self.engine.outbox
        intent = outbox.propose(
            run_id=self.run_id,
            step=self.step,
            slot=slot,
            action=action,
            payload=payload,
            proposed_by=f"workflow:{self.workflow}",
        )
        try:
            return outbox.process(intent["id"], fence=self.fence)
        except IntentWaiting as exc:
            raise WaitingApproval(exc.approval) from None
        except IntentRetry as exc:
            raise StepRetry(f"{action}: {exc}", delay=exc.delay, kind="tool_retries_exhausted") from None
        except IntentFailed as exc:
            raise StepFatal(
                f"{action}: {exc}", kind=exc.kind, details={"intent_id": exc.intent["id"], "action": action}
            ) from None

    def decide(self, key: str, question: str, options: list[str], context: dict[str, Any] | None = None) -> str:
        """Ask a person to choose. Returns the choice once made; until then the step waits."""
        return self.engine._decision(self, key, question, options, context or {})

    async def run_agent(self, agent_key: str, prompt: str, **extra: Any) -> Any:
        return await self.engine.agent_runtime().run(self, agent_key, prompt, **extra)

    def start_workflow(self, workflow: str, input: dict[str, Any], dedup_key: str | None = None) -> tuple[str, bool]:
        return self.engine.start_run(
            workflow, input, dedup_key=dedup_key, parent_run_id=self.run_id, actor=f"workflow:{self.workflow}"
        )


# ---- engine ----------------------------------------------------------------------


def _step_row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["depends_on"] = loads(out["depends_on"], [])
    out["output"] = loads(out.pop("output_json"), None)
    out["notes"] = loads(out.pop("notes_json"), [])
    return out


class Engine:
    def __init__(
        self,
        settings: Settings,
        workflows: list[WorkflowDef] | None = None,
        *,
        adapters: AdapterRegistry | None = None,
        reads: Any = None,
        clock: Callable[[], float] = time.time,
        worker_id: str | None = None,
    ):
        self.settings = settings
        self.clock = clock
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{new_id('w')[-6:]}"
        self.conn = db.connect(settings.db_path)
        self.audit = AuditLog(self.conn, clock)
        self.policy = Policy(settings.policy)
        self.approvals = Approvals(self.conn, settings.approval_secret, self.policy, self.audit, clock)
        self.costs = CostMeter(self.conn, settings.pricing, self.audit, clock)
        self.reporter = Reporter(self.conn, self.audit, clock, notify=self._notify_exception)
        if adapters is None:
            from .adapters.sandbox_adapters import sandbox_adapters
            from .sandbox import SandboxApps

            sandbox = SandboxApps(settings.sandbox_db_path, clock)
            sandbox.seed()
            adapters = AdapterRegistry(sandbox_adapters(sandbox))
            reads = reads or sandbox
        self.adapters = adapters
        self.reads = reads
        self.outbox = Outbox(
            self.conn,
            adapters,
            self.policy,
            self.approvals,
            self.audit,
            clock=clock,
            retry_base=settings.retry_base_seconds,
            retry_max=settings.retry_max_seconds,
            verify_attempts=settings.verify_attempts,
            on_approval_requested=self._notify_approval,
        )
        if workflows is None:
            from .workflows import default_workflows

            workflows = default_workflows()
        self.workflows = {w.name: w for w in workflows}
        self._runtime: Any = None

    # ---- lifecycle -------------------------------------------------------------
    def agent_runtime(self) -> Any:
        if self._runtime is None:
            from .specialists.runtime import AgentRuntime

            self._runtime = AgentRuntime(self)
        return self._runtime

    async def aclose(self) -> None:
        if self._runtime is not None:
            await self._runtime.aclose()
            self._runtime = None

    async def __aenter__(self) -> Engine:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def close(self) -> None:
        self.conn.close()

    # ---- runs --------------------------------------------------------------------
    def start_run(
        self,
        workflow: str,
        input: dict[str, Any],
        *,
        dedup_key: str | None = None,
        parent_run_id: str | None = None,
        actor: str = "api",
    ) -> tuple[str, bool]:
        """Start a run, or return the existing one if this business event was already seen."""
        wf = self.workflows[workflow]
        dedup_key = dedup_key or (wf.dedup(input) if wf.dedup else None)
        now = self.clock()
        with tx(self.conn):
            if dedup_key:
                existing = self.conn.execute("SELECT id FROM runs WHERE dedup_key = ?", (dedup_key,)).fetchone()
                if existing:
                    self.audit.record(
                        "run.duplicate_suppressed", actor=actor, run_id=existing["id"], data={"dedup_key": dedup_key}
                    )
                    return existing["id"], False
            run_id = new_id("run")
            self.conn.execute(
                "INSERT INTO runs (id, workflow, version, dedup_key, status, input_json, parent_run_id, created_at, updated_at)"
                " VALUES (?,?,?,?, 'running', ?, ?, ?, ?)",
                (run_id, wf.name, wf.version, dedup_key, dumps(input), parent_run_id, now, now),
            )
            for position, s in enumerate(wf.steps):
                self.conn.execute(
                    "INSERT INTO steps (run_id, name, position, status, depends_on, max_attempts) VALUES (?,?,?, 'pending', ?, ?)",
                    (run_id, s.name, position, dumps(list(s.depends_on)), s.max_attempts),
                )
            self.audit.record(
                "run.started",
                actor=actor,
                run_id=run_id,
                data={
                    "workflow": wf.name,
                    "version": wf.version,
                    "dedup_key": dedup_key,
                    "parent_run_id": parent_run_id,
                },
            )
        return run_id, True

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["input"] = loads(out.pop("input_json"), {})
        return out

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self.get_run(r["id"]) for r in rows]  # type: ignore[misc]

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        return [
            _step_row(r) for r in self.conn.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY position", (run_id,))
        ]

    def step_output(self, run_id: str, step: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT status, output_json FROM steps WHERE run_id = ? AND name = ?", (run_id, step)
        ).fetchone()
        if row is None or row["status"] != "succeeded":
            raise KeyError(f"{step} has no output yet")
        return loads(row["output_json"], {})

    def child_runs(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id FROM runs WHERE parent_run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [self.get_run(r["id"]) for r in rows]  # type: ignore[misc]

    def run_view(self, run_id: str) -> dict[str, Any]:
        return {
            "run": self.get_run(run_id),
            "steps": self.steps(run_id),
            "intents": self.outbox.list(run_id=run_id),
            "approvals": self.approvals.list(status=None, run_id=run_id),
            "decisions": [dict(r) for r in self.conn.execute("SELECT * FROM decisions WHERE run_id = ?", (run_id,))],
            "exceptions": self.reporter.list(status=None, run_id=run_id),
            "costs": self.costs.breakdown(run_id),
            "cost_total_usd": self.costs.run_total(run_id),
            "children": [r["id"] for r in self.child_runs(run_id)],
        }

    # ---- scheduling ----------------------------------------------------------------
    async def tick(self) -> int:
        """One scheduling pass. Returns how many units of work made progress."""
        progressed = self._recover_expired_leases()
        while (claim := self._claim_next()) is not None:
            await self._run_step(*claim)
            progressed += 1
        progressed += self._dispatch_side_intents()
        self._refresh_runs()
        return progressed

    async def run_until_idle(self, timeout: float = 60.0, wait_for_leases: bool = False) -> None:
        """Tick until nothing is runnable; sleeps through short retry backoffs."""
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            if await self.tick():
                continue
            wake = self._next_wakeup(include_leases=wait_for_leases)
            if wake is None or wake > deadline:
                return
            await asyncio.sleep(max(0.0, wake - self.clock()) + 0.01)

    def _next_wakeup(self, include_leases: bool) -> float | None:
        times = [
            r[0]
            for r in self.conn.execute(
                "SELECT next_attempt_at FROM steps WHERE status = 'retry_wait' AND next_attempt_at IS NOT NULL"
                " UNION ALL SELECT next_attempt_at FROM intents WHERE origin != 'step' AND status = 'retry' AND next_attempt_at IS NOT NULL"
            )
        ]
        if include_leases:
            times += [r[0] for r in self.conn.execute("SELECT lease_expires_at FROM steps WHERE status = 'running'")]
        return min(times) if times else None

    def _recover_expired_leases(self) -> int:
        now = self.clock()
        recovered = []
        with tx(self.conn):
            rows = self.conn.execute(
                "SELECT * FROM steps WHERE status = 'running' AND lease_expires_at < ?", (now,)
            ).fetchall()
            for row in rows:
                changed = self.conn.execute(
                    "UPDATE steps SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL"
                    " WHERE run_id = ? AND name = ? AND lease_token = ? AND status = 'running'",
                    (row["run_id"], row["name"], row["lease_token"]),
                ).rowcount
                if changed:
                    self.audit.record(
                        "step.lease_expired",
                        actor=self.worker_id,
                        run_id=row["run_id"],
                        subject=row["name"],
                        data={
                            "previous_owner": row["lease_owner"],
                            "token": row["lease_token"],
                            "attempts": row["attempts"],
                        },
                    )
                    recovered.append(row)
        for row in recovered:
            self.reporter.raise_exception(
                kind="lease_recovered",
                severity="info",
                message=f"{row['name']} was interrupted on {row['lease_owner']} and has been resumed",
                run_id=row["run_id"],
                step=row["name"],
                dedup=f"{row['run_id']}|{row['name']}|lease|{row['lease_token']}",
            )
        return len(recovered)

    def _claim_next(self) -> tuple[dict[str, Any], dict[str, Any], Fence] | None:
        now = self.clock()
        with tx(self.conn):
            rows = self.conn.execute(
                "SELECT s.* FROM steps s JOIN runs r ON r.id = s.run_id"
                " WHERE r.status IN ('running', 'waiting')"
                " AND (s.status = 'pending' OR (s.status = 'retry_wait' AND s.next_attempt_at <= ?))"
                " ORDER BY r.created_at, s.position",
                (now,),
            ).fetchall()
            for row in rows:
                deps = loads(row["depends_on"], [])
                if deps:
                    statuses = {
                        r["name"]: r["status"]
                        for r in self.conn.execute(
                            f"SELECT name, status FROM steps WHERE run_id = ? AND name IN ({','.join('?' * len(deps))})",
                            (row["run_id"], *deps),
                        )
                    }
                    if any(statuses.get(d) != "succeeded" for d in deps):
                        continue
                token = row["lease_token"] + 1
                changed = self.conn.execute(
                    "UPDATE steps SET status = 'running', lease_owner = ?, lease_token = ?, lease_expires_at = ?,"
                    " attempts = attempts + 1, next_attempt_at = NULL, started_at = COALESCE(started_at, ?)"
                    " WHERE run_id = ? AND name = ? AND lease_token = ? AND status = ?",
                    (
                        self.worker_id,
                        token,
                        now + self.settings.lease_seconds,
                        now,
                        row["run_id"],
                        row["name"],
                        row["lease_token"],
                        row["status"],
                    ),
                ).rowcount
                if not changed:
                    continue
                self.audit.record(
                    "step.claimed",
                    actor=self.worker_id,
                    run_id=row["run_id"],
                    subject=row["name"],
                    data={"attempt": row["attempts"] + 1, "token": token},
                )
                step = _step_row(
                    self.conn.execute(
                        "SELECT * FROM steps WHERE run_id = ? AND name = ?", (row["run_id"], row["name"])
                    ).fetchone()
                )
                return self.get_run(row["run_id"]), step, Fence(row["run_id"], row["name"], token)  # type: ignore[return-value]
        return None

    async def _run_step(self, run: dict[str, Any], step: dict[str, Any], fence: Fence) -> None:
        wf = self.workflows[run["workflow"]]
        definition = wf.step(step["name"])
        ctx = StepContext(self, run, step, fence)
        try:
            result = await definition.handler(ctx)
        except WaitingApproval as exc:
            self._park(fence, f"approval:{exc.approval['id']}", str(exc))
        except WaitingDecision as exc:
            self._park(fence, f"decision:{exc.decision_id}", str(exc))
        except StepRetry as exc:
            self._retry(run, step, fence, str(exc), exc.delay, exc.kind)
        except StepFatal as exc:
            self._fail(run, step, fence, exc.kind, str(exc), exc.severity, exc.details)
        except BudgetExceeded as exc:
            self._fail(
                run,
                step,
                fence,
                "budget_exceeded",
                str(exc),
                "high",
                {"scope": exc.scope, "spent": exc.spent, "limit": exc.limit},
            )
        except PolicyViolation as exc:
            self._fail(run, step, fence, "policy_violation", str(exc), "high", {"kind": exc.kind})
        except LeaseLost as exc:
            self.audit.record(
                "step.lease_lost",
                actor=self.worker_id,
                run_id=run["id"],
                subject=step["name"],
                data={"reason": str(exc)},
            )
        except SimulatedCrash:
            raise
        except Exception as exc:  # model errors, schema mismatches, bugs: retry, then report
            self._retry(run, step, fence, f"{type(exc).__name__}: {exc}", None, "agent_error")
        else:
            try:
                if isinstance(result, Rewind):
                    self._rewind(run, step, fence, result)
                else:
                    self._complete(run, step, fence, result.output, ctx.proposals)
            except LeaseLost as exc:  # a newer worker owns the step; drop this result
                self.audit.record(
                    "step.result_discarded",
                    actor=self.worker_id,
                    run_id=run["id"],
                    subject=step["name"],
                    data={"reason": str(exc)},
                )

    def _release(self, fence: Fence, **fields: Any) -> bool:
        fields.update(lease_owner=None, lease_expires_at=None)
        cols = ", ".join(f"{k} = ?" for k in fields)
        return bool(
            self.conn.execute(
                f"UPDATE steps SET {cols} WHERE run_id = ? AND name = ? AND lease_token = ? AND status = 'running'",
                (*fields.values(), fence.run_id, fence.step, fence.token),
            ).rowcount
        )

    def _complete(
        self,
        run: dict[str, Any],
        step: dict[str, Any],
        fence: Fence,
        output: dict[str, Any],
        proposals: list[dict[str, Any]],
    ) -> None:
        with tx(self.conn):
            fence.check(self.conn)
            for p in proposals:  # agent proposals commit atomically with the step's output
                self.outbox.propose(run_id=run["id"], step=step["name"], origin="agent", **p)
            self._release(
                fence, status="succeeded", output_json=dumps(output), finished_at=self.clock(), last_error=None
            )
            self.audit.record(
                "step.succeeded",
                actor=self.worker_id,
                run_id=run["id"],
                subject=step["name"],
                data={"attempt": step["attempts"], "output_keys": sorted(output), "agent_proposals": len(proposals)},
            )

    def _park(self, fence: Fence, waiting_on: str, reason: str) -> None:
        with tx(self.conn):
            if self._release(fence, status="waiting", waiting_on=waiting_on, last_error=None):
                self.audit.record(
                    "step.waiting",
                    actor=self.worker_id,
                    run_id=fence.run_id,
                    subject=fence.step,
                    data={"on": waiting_on, "reason": reason},
                )

    def _retry(
        self, run: dict[str, Any], step: dict[str, Any], fence: Fence, message: str, delay: float | None, kind: str
    ) -> None:
        if step["attempts"] >= step["max_attempts"]:
            self._fail(run, step, fence, kind, f"gave up after {step['attempts']} attempts: {message}", "high", {})
            return
        if delay is None:
            delay = min(self.settings.retry_max_seconds, self.settings.retry_base_seconds * 2 ** (step["attempts"] - 1))
        with tx(self.conn):
            if self._release(fence, status="retry_wait", next_attempt_at=self.clock() + delay, last_error=message):
                self.audit.record(
                    "step.retry_scheduled",
                    actor=self.worker_id,
                    run_id=run["id"],
                    subject=step["name"],
                    data={"attempt": step["attempts"], "retry_in_s": round(delay, 2), "error": message, "kind": kind},
                )

    def _fail(
        self,
        run: dict[str, Any],
        step: dict[str, Any],
        fence: Fence,
        kind: str,
        message: str,
        severity: str,
        details: dict[str, Any],
    ) -> None:
        wf = self.workflows[run["workflow"]]
        blocked = sorted(wf.dependents(step["name"]))
        with tx(self.conn):
            if not self._release(fence, status="failed", last_error=message, finished_at=self.clock()):
                return
            if blocked:
                self.conn.execute(
                    f"UPDATE steps SET status = 'blocked' WHERE run_id = ? AND name IN ({','.join('?' * len(blocked))})"
                    " AND status IN ('pending', 'retry_wait', 'waiting')",
                    (run["id"], *blocked),
                )
            self.audit.record(
                "step.failed",
                actor=self.worker_id,
                run_id=run["id"],
                subject=step["name"],
                data={"kind": kind, "error": message, "blocked": blocked},
            )
        info_only = kind in ("approval_rejected",)
        self.reporter.raise_exception(
            kind=kind,
            severity="low" if info_only else severity,
            message=f"{run['workflow']}/{step['name']}: {message}",
            run_id=run["id"],
            step=step["name"],
            details={**details, "blocked_steps": blocked},
        )

    def _rewind(self, run: dict[str, Any], step: dict[str, Any], fence: Fence, rewind: Rewind) -> None:
        if step["revisions"] >= self.settings.max_review_revisions:
            self._fail(
                run,
                step,
                fence,
                "review_rejected",
                f"still failing review after {step['revisions']} revision(s): {rewind.note}",
                "high",
                rewind.details,
            )
            return
        with tx(self.conn):
            fence.check(self.conn)
            target = self.conn.execute(
                "SELECT notes_json FROM steps WHERE run_id = ? AND name = ?", (run["id"], rewind.target)
            ).fetchone()
            notes = loads(target["notes_json"], []) + [
                {"from": step["name"], "note": rewind.note, "details": rewind.details}
            ]
            self.conn.execute(
                "UPDATE steps SET status = 'pending', output_json = NULL, notes_json = ?, attempts = 0, finished_at = NULL"
                " WHERE run_id = ? AND name = ?",
                (dumps(notes), run["id"], rewind.target),
            )
            self._release(fence, status="pending", attempts=0, revisions=step["revisions"] + 1, last_error=rewind.note)
            self.audit.record(
                "step.rewound",
                actor=self.worker_id,
                run_id=run["id"],
                subject=rewind.target,
                data={"by": step["name"], "revision": step["revisions"] + 1, "note": rewind.note},
            )

    def _dispatch_side_intents(self) -> int:
        """Execute intents that are not owned by a step: agent proposals and system notices."""
        progressed = 0
        rows = self.conn.execute(
            "SELECT id, status FROM intents WHERE origin != 'step'"
            " AND status IN ('proposed', 'approved', 'retry', 'executing', 'awaiting_approval') ORDER BY created_at"
        ).fetchall()
        for row in rows:
            try:
                intent = self.outbox.process(row["id"])
            except IntentWaiting:
                continue
            except IntentRetry:
                continue
            except IntentFailed as exc:
                progressed += 1
                self.reporter.raise_exception(
                    kind=exc.kind,
                    severity="low" if exc.kind == "approval_rejected" else "high",
                    message=f"side action {exc.intent['action']} ({exc.intent['slot']}): {exc}",
                    run_id=exc.intent["run_id"],
                    step=exc.intent["step"],
                    dedup=f"intent|{exc.intent['id']}",
                )
                continue
            if intent["status"] != row["status"]:
                progressed += 1
        return progressed

    def _refresh_runs(self) -> None:
        with tx(self.conn):
            for run in self.conn.execute(
                "SELECT id, status FROM runs WHERE status IN ('running', 'waiting')"
            ).fetchall():
                rows = self.conn.execute(
                    "SELECT name, status, depends_on FROM steps WHERE run_id = ?", (run["id"],)
                ).fetchall()
                status = {r["name"]: r["status"] for r in rows}
                runnable = any(
                    r["status"] in ("running", "retry_wait")
                    or (r["status"] == "pending" and all(status[d] == "succeeded" for d in loads(r["depends_on"], [])))
                    for r in rows
                )
                if all(s == "succeeded" for s in status.values()):
                    new = "succeeded"
                elif runnable:
                    new = "running"
                elif "waiting" in status.values():
                    new = "waiting"
                else:
                    new = "failed"
                if new != run["status"]:
                    finished = self.clock() if new in ("succeeded", "failed") else None
                    self.conn.execute(
                        "UPDATE runs SET status = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                        (new, self.clock(), finished, run["id"]),
                    )
                    self.audit.record(f"run.{new}", actor=self.worker_id, run_id=run["id"])

    # ---- people ----------------------------------------------------------------------
    def decide_approval(
        self, approval_id: str, principal: str, decision: str, note: str | None = None
    ) -> dict[str, Any]:
        approval = self.approvals.decide(approval_id, principal, decision, note)  # type: ignore[arg-type]
        self._wake(f"approval:{approval_id}", principal)
        return approval

    def answer_decision(self, decision_id: str, principal: str, choice: str) -> None:
        if not self.policy.principal_roles(principal):
            raise PermissionError(f"{principal} is not a known decision maker")
        with tx(self.conn):
            row = self.conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
            if row is None or row["status"] != "pending":
                raise ValueError("no open decision with that id")
            if choice not in loads(row["options_json"], []):
                raise ValueError(f"choice must be one of {loads(row['options_json'], [])}")
            self.conn.execute(
                "UPDATE decisions SET status = 'decided', choice = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                (choice, principal, self.clock(), decision_id),
            )
            self.audit.record(
                "decision.made", actor=principal, run_id=row["run_id"], subject=decision_id, data={"choice": choice}
            )
            self.conn.execute(
                "UPDATE exceptions SET status = 'resolved', resolved_at = ?, resolved_by = ?"
                " WHERE run_id = ? AND step = ? AND kind = 'needs_decision' AND status = 'open'",
                (self.clock(), principal, row["run_id"], row["step"]),
            )
        self._wake(f"decision:{decision_id}", principal)

    def _wake(self, waiting_on: str, actor: str) -> None:
        with tx(self.conn):
            rows = self.conn.execute(
                "SELECT run_id, name FROM steps WHERE waiting_on = ? AND status = 'waiting'", (waiting_on,)
            ).fetchall()
            for r in rows:
                self.conn.execute(
                    "UPDATE steps SET status = 'pending', waiting_on = NULL WHERE run_id = ? AND name = ?",
                    (r["run_id"], r["name"]),
                )
                self.conn.execute(
                    "UPDATE runs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'waiting'",
                    (self.clock(), r["run_id"]),
                )
                self.audit.record(
                    "step.woken", actor=actor, run_id=r["run_id"], subject=r["name"], data={"by": waiting_on}
                )

    def _decision(self, ctx: StepContext, key: str, question: str, options: list[str], context: dict[str, Any]) -> str:
        created = None
        with tx(self.conn):
            row = self.conn.execute(
                "SELECT * FROM decisions WHERE run_id = ? AND step = ? AND key = ?", (ctx.run_id, ctx.step, key)
            ).fetchone()
            if row and row["status"] == "decided":
                return row["choice"]
            if row is None:
                decision_id = new_id("dec")
                self.conn.execute(
                    "INSERT INTO decisions (id, run_id, step, key, question, options_json, status, created_at) VALUES (?,?,?,?,?,?, 'pending', ?)",
                    (decision_id, ctx.run_id, ctx.step, key, question, dumps(options), self.clock()),
                )
                self.audit.record(
                    "decision.requested",
                    actor="engine",
                    run_id=ctx.run_id,
                    subject=decision_id,
                    data={"question": question, "options": options, "context": context},
                )
                created = decision_id
            else:
                decision_id = row["id"]
        if created:
            self.reporter.raise_exception(
                kind="needs_decision",
                severity="high",
                message=question,
                run_id=ctx.run_id,
                step=ctx.step,
                details={"decision_id": decision_id, "options": options, **context},
            )
        raise WaitingDecision(decision_id, question)

    def retry_step(self, run_id: str, step_name: str, actor: str) -> None:
        """Operator retry after fixing a cause. Failed intents are reconciled, never blindly resent."""
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        wf = self.workflows[run["workflow"]]
        for intent in self.outbox.list(run_id=run_id):
            if intent["step"] == step_name and intent["status"] == "failed":
                self.outbox.reset_for_retry(intent["id"], actor)
        with tx(self.conn):
            self.conn.execute(
                "UPDATE steps SET status = 'pending', attempts = 0, last_error = NULL, finished_at = NULL"
                " WHERE run_id = ? AND name = ? AND status IN ('failed', 'blocked', 'retry_wait')",
                (run_id, step_name),
            )
            downstream = sorted(wf.dependents(step_name))
            if downstream:
                self.conn.execute(
                    f"UPDATE steps SET status = 'pending' WHERE run_id = ? AND status = 'blocked' AND name IN ({','.join('?' * len(downstream))})",
                    (run_id, *downstream),
                )
            self.conn.execute(
                "UPDATE runs SET status = 'running', finished_at = NULL, updated_at = ? WHERE id = ?",
                (self.clock(), run_id),
            )
            self.audit.record(
                "step.retry_requested", actor=actor, run_id=run_id, subject=step_name, data={"unblocked": downstream}
            )
        self.reporter.resolve_for_step(run_id, step_name, actor)

    # ---- notifications (themselves internal-routine side effects) -------------------
    def _notify_approval(self, intent: dict[str, Any], approval: dict[str, Any]) -> None:
        roles = ", ".join(approval["required_roles"])
        text = (
            f"Approval needed ({approval['risk_class'].replace('_', ' ')}): {intent['action']} for run {intent['run_id']}.\n"
            f"{approval['preview'].get('summary', '')}\n"
            f"Who can approve: {roles}. Review: /approvals/{approval['id']} or `switchyard approve {approval['id']} --as <you>`"
        )
        self.outbox.propose(
            run_id=intent["run_id"],
            step="_notify",
            slot=f"approval:{approval['id']}",
            action="chat.post",
            payload={"channel": "#approvals", "text": text},
            proposed_by="engine",
            origin="system",
        )

    def _notify_exception(self, record: dict[str, Any]) -> None:
        text = f"[{record['severity']}] {record['kind']}: {record['message']}\nNext: {record.get('suggested_action') or 'see console'}"
        self.outbox.propose(
            run_id=record["run_id"] or "_system",
            step="_notify",
            slot=f"exception:{record['id']}",
            action="chat.post",
            payload={"channel": "#ops-exceptions", "text": text},
            proposed_by="engine",
            origin="system",
        )
