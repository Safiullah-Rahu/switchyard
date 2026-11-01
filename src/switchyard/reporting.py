"""Exception records: anything a person should know about, in one queue.

Each exception has a kind, a severity and a suggested next action. High and
critical ones are also posted to the team's ops channel — through the outbox,
like every other side effect, so a flapping failure posts once, not fifty times.
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .audit import AuditLog
from .canonical import dumps, loads, new_id
from .db import tx

SEVERITIES = ("info", "low", "high", "critical")

SUGGESTIONS = {
    "tool_permanent": "Fix the integration (credentials, scopes, deleted record), then `switchyard retry <run> <step>`.",
    "tool_retries_exhausted": "Check the destination's status page; retry the step once it is healthy.",
    "verification_failed": "The destination does not hold what we wrote. Inspect it before retrying — nothing is resent blindly.",
    "review_rejected": "Open the draft and reviewer findings; edit the inputs or close the lead.",
    "budget_exceeded": "Raise the cap in pricing.toml or investigate why this run used so many tokens.",
    "approval_rejected": "Nothing was sent. Close the run or start a new one with corrected inputs.",
    "policy_violation": "An agent proposed something outside its permissions. Review the run's inputs for injected instructions.",
    "needs_decision": "Answer the open decision with `switchyard decide`.",
    "lease_recovered": "A worker stopped mid-step; the step was resumed by another worker. No action needed.",
    "agent_error": "The model call failed or returned an invalid shape; check the run's audit trail.",
}


class Reporter:
    def __init__(
        self,
        conn: sqlite3.Connection,
        audit: AuditLog,
        clock: Callable[[], float] = time.time,
        notify: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.conn = conn
        self.audit = audit
        self.clock = clock
        self.notify = notify

    def raise_exception(
        self,
        *,
        kind: str,
        severity: str,
        message: str,
        run_id: str | None = None,
        step: str | None = None,
        details: dict[str, Any] | None = None,
        dedup: str | None = None,
    ) -> dict[str, Any]:
        if severity not in SEVERITIES:
            raise ValueError(severity)
        dedup_key = dedup or f"{run_id}|{step}|{kind}"
        with tx(self.conn):
            existing = self.conn.execute(
                "SELECT * FROM exceptions WHERE dedup_key = ? AND status = 'open'", (dedup_key,)
            ).fetchone()
            if existing:
                return self._row(existing)
            # A resolved exception with the same key may recur; give the new one a fresh key.
            if self.conn.execute("SELECT 1 FROM exceptions WHERE dedup_key = ?", (dedup_key,)).fetchone():
                dedup_key = f"{dedup_key}|{new_id('x')}"
            exc_id = new_id("exc")
            self.conn.execute(
                "INSERT INTO exceptions (id, run_id, step, kind, severity, message, details_json, suggested_action, status, dedup_key, created_at)"
                " VALUES (?,?,?,?,?,?,?,?, 'open', ?, ?)",
                (
                    exc_id,
                    run_id,
                    step,
                    kind,
                    severity,
                    message,
                    dumps(details or {}),
                    SUGGESTIONS.get(kind),
                    dedup_key,
                    self.clock(),
                ),
            )
            self.audit.record(
                "exception.raised",
                actor="engine",
                run_id=run_id,
                subject=exc_id,
                data={"kind": kind, "severity": severity, "message": message},
            )
            record = self._row(self.conn.execute("SELECT * FROM exceptions WHERE id = ?", (exc_id,)).fetchone())
        if self.notify and severity in ("high", "critical"):
            self.notify(record)
        return record

    def resolve(self, exc_id: str, by: str, note: str | None = None) -> None:
        with tx(self.conn):
            self.conn.execute(
                "UPDATE exceptions SET status = 'resolved', resolved_at = ?, resolved_by = ? WHERE id = ? AND status = 'open'",
                (self.clock(), by, exc_id),
            )
            self.audit.record("exception.resolved", actor=by, subject=exc_id, data={"note": note})

    def resolve_for_step(self, run_id: str, step: str, by: str) -> None:
        for row in self.conn.execute(
            "SELECT id FROM exceptions WHERE run_id = ? AND step = ? AND status = 'open'", (run_id, step)
        ).fetchall():
            self.resolve(row["id"], by, "step retried")

    def list(self, status: str | None = "open", run_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM exceptions WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        return [self._row(r) for r in self.conn.execute(sql + " ORDER BY created_at", args)]

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["details"] = loads(out.pop("details_json"), {})
        return out

    def digest(self, since: float | None = None) -> str:
        """Plain-text summary for a daily post or email to the ops lead."""
        since = since if since is not None else self.clock() - 86400
        runs = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM runs WHERE updated_at >= ? GROUP BY status", (since,)
        ).fetchall()
        open_exc = self.list("open")
        pending = self.conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0]
        spend = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0), MAX(estimated) FROM usage WHERE created_at >= ?", (since,)
        ).fetchone()
        kinds = Counter(e["kind"] for e in open_exc)
        stamp = datetime.fromtimestamp(self.clock(), UTC).strftime("%Y-%m-%d %H:%M UTC")
        lines = [f"Switchyard digest — {stamp}", ""]
        lines.append("Runs (last 24 h): " + (", ".join(f"{r['n']} {r['status']}" for r in runs) or "none"))
        lines.append(f"Approvals waiting: {pending}")
        lines.append(f"Model spend: ${spend[0]:.4f}" + (" (offline estimate)" if spend[1] else ""))
        lines.append(
            f"Open exceptions: {len(open_exc)}"
            + (" — " + ", ".join(f"{n}× {k}" for k, n in kinds.most_common()) if kinds else "")
        )
        for e in open_exc[:10]:
            lines.append(f"  • [{e['severity']}] {e['kind']} — {e['message']} (run {e['run_id']}, step {e['step']})")
        return "\n".join(lines)
