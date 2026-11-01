"""The outbox: the only code path that changes anything outside Switchyard.

An *intent* is one logical side effect ("send the proposal email for run X").
Its idempotency key is derived from (run, step, slot) — never from model output
— so re-running a step after a crash finds the same intent instead of making a
second one. The state machine:

    proposed ──policy──► awaiting_approval ──approved──► executing ──► executed ──► verified
        │                      │ rejected/expired                │ (crash: lookup by key)
        └──────────────────────┴──────► rejected                 └──► failed (permanent / verify)

`executing` is written *before* the remote call. Finding an intent in that
state later means we do not know whether the write landed, so the adapter's
`lookup` is asked first; only if the destination has nothing do we call again.
"""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .adapters.base import (
    AdapterRegistry,
    AmbiguousOutcome,
    PermanentError,
    Receipt,
    TransientError,
    Verification,
)
from .approvals import Approvals
from .audit import AuditLog
from .canonical import dumps, idempotency_key, loads, new_id, payload_hash
from .db import tx
from .policy import Policy, PolicyViolation

TERMINAL = {"verified", "rejected", "failed"}


class IntentWaiting(Exception):
    def __init__(self, intent: dict[str, Any], approval: dict[str, Any]):
        super().__init__(f"{intent['action']} is waiting for approval {approval['id']}")
        self.intent = intent
        self.approval = approval


class IntentRetry(Exception):
    def __init__(self, intent: dict[str, Any], message: str, delay: float):
        super().__init__(message)
        self.intent = intent
        self.delay = delay


class IntentFailed(Exception):
    def __init__(self, intent: dict[str, Any], message: str, kind: str):
        super().__init__(message)
        self.intent = intent
        self.kind = kind


class LeaseLost(Exception):
    """Another worker took over this step; this worker must stop touching it."""


@dataclass
class Fence:
    """Proof that the caller still holds the step lease (a fencing token)."""

    run_id: str
    step: str
    token: int

    def check(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT lease_token, status FROM steps WHERE run_id = ? AND name = ?", (self.run_id, self.step)
        ).fetchone()
        if row is None or row["lease_token"] != self.token or row["status"] != "running":
            raise LeaseLost(f"lease on {self.run_id}/{self.step} is no longer held (token {self.token})")


def intent_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["payload"] = loads(out.pop("payload_json"), {})
    out["reasons"] = loads(out.pop("reasons_json"), [])
    out["receipt"] = loads(out.pop("receipt_json"), None)
    out["verification"] = loads(out.pop("verification_json"), None)
    return out


class Outbox:
    def __init__(
        self,
        conn: sqlite3.Connection,
        adapters: AdapterRegistry,
        policy: Policy,
        approvals: Approvals,
        audit: AuditLog,
        *,
        clock: Callable[[], float] = time.time,
        retry_base: float = 2.0,
        retry_max: float = 300.0,
        verify_attempts: int = 3,
        on_approval_requested: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ):
        self.conn = conn
        self.adapters = adapters
        self.policy = policy
        self.approvals = approvals
        self.audit = audit
        self.clock = clock
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.verify_attempts = verify_attempts
        self.on_approval_requested = on_approval_requested

    # ---- queries -----------------------------------------------------------
    def get(self, intent_id: str) -> dict[str, Any] | None:
        return intent_row(self.conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone())

    def by_key(self, key: str) -> dict[str, Any] | None:
        return intent_row(self.conn.execute("SELECT * FROM intents WHERE idem_key = ?", (key,)).fetchone())

    def list(self, run_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM intents WHERE 1=1", []
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        if status:
            sql += " AND status = ?"
            args.append(status)
        return [intent_row(r) for r in self.conn.execute(sql + " ORDER BY created_at", args)]  # type: ignore[misc]

    def _set(self, intent_id: str, **fields: Any) -> None:
        fields["updated_at"] = self.clock()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE intents SET {cols} WHERE id = ?", (*fields.values(), intent_id))

    # ---- proposing -----------------------------------------------------------
    def propose(
        self,
        *,
        run_id: str,
        step: str,
        slot: str,
        action: str,
        payload: dict[str, Any],
        proposed_by: str,
        origin: str = "step",
    ) -> dict[str, Any]:
        """Record (or find) the intent for this logical side effect. Does not execute it."""
        classification = self.policy.classify(action, payload)
        key = idempotency_key(run_id, step, slot)
        digest = payload_hash(payload)
        with tx(self.conn):
            existing = self.by_key(key)
            if existing:
                if existing["payload_hash"] == digest:
                    return existing
                if existing["status"] in ("executing", "executed", "verified"):
                    # The effect already happened with the earlier payload. Never redo it.
                    self.audit.record(
                        "intent.revision_ignored",
                        actor=proposed_by,
                        run_id=run_id,
                        subject=existing["id"],
                        data={"reason": f"already {existing['status']}", "new_payload_hash": digest},
                    )
                    return existing
                self._set(
                    existing["id"],
                    payload_json=dumps(payload),
                    payload_hash=digest,
                    risk_class=classification.risk_class,
                    reasons_json=dumps(list(classification.reasons)),
                    status="proposed",
                    last_error=None,
                )
                self.audit.record(
                    "intent.revised",
                    actor=proposed_by,
                    run_id=run_id,
                    subject=existing["id"],
                    data={
                        "old_hash": existing["payload_hash"],
                        "new_hash": digest,
                        "risk_class": classification.risk_class,
                    },
                )
                return self.get(existing["id"])  # type: ignore[return-value]
            intent_id = new_id("int")
            now = self.clock()
            self.conn.execute(
                "INSERT INTO intents (id, idem_key, run_id, step, slot, origin, proposed_by, action, app, payload_json,"
                " payload_hash, risk_class, reasons_json, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'proposed', ?, ?)",
                (
                    intent_id,
                    key,
                    run_id,
                    step,
                    slot,
                    origin,
                    proposed_by,
                    action,
                    classification.app,
                    dumps(payload),
                    digest,
                    classification.risk_class,
                    dumps(list(classification.reasons)),
                    now,
                    now,
                ),
            )
            self.audit.record(
                "intent.proposed",
                actor=proposed_by,
                run_id=run_id,
                subject=intent_id,
                data={
                    "action": action,
                    "slot": slot,
                    "idem_key": key,
                    "payload_hash": digest,
                    "risk_class": classification.risk_class,
                    "reasons": list(classification.reasons),
                    "requires_approval": classification.requires_approval,
                },
            )
            return self.get(intent_id)  # type: ignore[return-value]

    # ---- processing ----------------------------------------------------------
    def process(self, intent_id: str, fence: Fence | None = None) -> dict[str, Any]:
        """Drive one intent as far as it can go right now. Raises IntentWaiting / IntentRetry / IntentFailed."""
        intent = self.get(intent_id)
        if intent is None:
            raise KeyError(intent_id)
        if intent["status"] == "verified":
            return intent
        if intent["status"] in ("rejected", "failed"):
            raise IntentFailed(intent, intent["last_error"] or intent["status"], kind=intent["status"])

        if intent["status"] in ("proposed", "awaiting_approval", "retry"):
            self._gate(intent)
            intent = self.get(intent_id)  # type: ignore[assignment]

        adapter = self.adapters.for_app(intent["app"])
        receipt: Receipt | None = None

        if intent["status"] == "executing":
            # We crashed or timed out mid-call last time: ask the destination before acting again.
            receipt = adapter.lookup(intent["action"], intent["idem_key"], intent["payload"])
            with tx(self.conn):
                self.audit.record(
                    "intent.reconciled",
                    actor="engine",
                    run_id=intent["run_id"],
                    subject=intent["id"],
                    data={
                        "found_at_destination": receipt is not None,
                        "receipt": receipt.as_dict() if receipt else None,
                    },
                )
                if receipt is not None:
                    self._set(intent["id"], status="executed", receipt_json=dumps(receipt.as_dict()))
            intent = self.get(intent_id)  # type: ignore[assignment]

        if intent["status"] in ("approved", "executing"):
            receipt = self._execute(intent, adapter, fence)
            intent = self.get(intent_id)  # type: ignore[assignment]

        if intent["status"] == "executed":
            self._verify(intent, adapter)
            intent = self.get(intent_id)  # type: ignore[assignment]
        return intent

    def _gate(self, intent: dict[str, Any]) -> None:
        """Re-classify from the stored payload and enforce approval. Runs every time, not once."""
        if intent["status"] == "retry" and intent["next_attempt_at"] and intent["next_attempt_at"] > self.clock():
            raise IntentRetry(
                intent, intent["last_error"] or "waiting to retry", intent["next_attempt_at"] - self.clock()
            )
        try:
            classification = self.policy.classify(intent["action"], intent["payload"])
        except PolicyViolation as exc:
            with tx(self.conn):
                self._set(intent["id"], status="failed", last_error=str(exc))
                self.audit.record(
                    "intent.refused",
                    actor="policy",
                    run_id=intent["run_id"],
                    subject=intent["id"],
                    data={"reason": str(exc)},
                )
            raise IntentFailed(intent, str(exc), kind="policy_violation") from exc

        if classification.requires_approval:
            check = self.approvals.check(intent)
            if check.status == "approved":
                with tx(self.conn):
                    self._set(intent["id"], status="approved")
                    self.audit.record(
                        "intent.gate_passed",
                        actor="policy",
                        run_id=intent["run_id"],
                        subject=intent["id"],
                        data={
                            "risk_class": classification.risk_class,
                            "approval_id": check.approval["id"],
                            "by": check.approval["decided_by"],
                        },  # type: ignore[index]
                    )
                return
            if check.status == "rejected":
                with tx(self.conn):
                    self._set(intent["id"], status="rejected", last_error=check.reason)
                    self.audit.record(
                        "intent.rejected",
                        actor="policy",
                        run_id=intent["run_id"],
                        subject=intent["id"],
                        data={"reason": check.reason},
                    )
                raise IntentFailed(self.get(intent["id"]), check.reason, kind="approval_rejected")  # type: ignore[arg-type]
            # missing / pending / expired / invalid → (re)request and wait.
            with tx(self.conn):
                if check.status in ("expired", "invalid") and check.approval:
                    self.conn.execute(
                        "UPDATE approvals SET status = 'superseded' WHERE id = ? AND status != 'expired'",
                        (check.approval["id"],),
                    )
                    self.audit.record(
                        "approval.not_usable",
                        actor="policy",
                        run_id=intent["run_id"],
                        subject=check.approval["id"],
                        data={"status": check.status, "reason": check.reason},
                    )
                approval = self.approvals.request(intent, classification, preview=self._preview(intent, classification))
                created = approval["status"] == "pending" and check.status != "pending"
                self._set(intent["id"], status="awaiting_approval")
            if created and self.on_approval_requested:
                self.on_approval_requested(intent, approval)
            raise IntentWaiting(intent, approval)

        if intent["status"] != "approved":
            with tx(self.conn):
                self._set(intent["id"], status="approved")
                self.audit.record(
                    "intent.gate_passed",
                    actor="policy",
                    run_id=intent["run_id"],
                    subject=intent["id"],
                    data={"risk_class": classification.risk_class, "approval": "not required"},
                )

    def _preview(self, intent: dict[str, Any], classification: Any) -> dict[str, Any]:
        payload = intent["payload"]
        return {
            "action": intent["action"],
            "risk_class": classification.risk_class,
            "reasons": list(classification.reasons),
            "summary": payload.get("subject") or payload.get("title") or payload.get("text", "")[:120],
            "to": payload.get("to") or payload.get("customer_email") or payload.get("channel"),
            "payload": payload,
        }

    def _execute(self, intent: dict[str, Any], adapter: Any, fence: Fence | None) -> Receipt | None:
        with tx(self.conn):
            if fence:
                fence.check(self.conn)
            self._set(intent["id"], status="executing", attempts=intent["attempts"] + 1)
            self.audit.record(
                "intent.executing",
                actor="engine",
                run_id=intent["run_id"],
                subject=intent["id"],
                data={"action": intent["action"], "attempt": intent["attempts"] + 1, "idem_key": intent["idem_key"]},
            )
        try:
            receipt = adapter.execute(intent["action"], intent["payload"], intent["idem_key"])
        except TransientError as exc:
            delay = self._backoff(intent["attempts"] + 1)
            with tx(self.conn):
                self._set(intent["id"], status="retry", last_error=str(exc), next_attempt_at=self.clock() + delay)
                self.audit.record(
                    "intent.transient_error",
                    actor="engine",
                    run_id=intent["run_id"],
                    subject=intent["id"],
                    data={"error": str(exc), "retry_in_s": round(delay, 2)},
                )
            raise IntentRetry(self.get(intent["id"]), str(exc), delay) from exc  # type: ignore[arg-type]
        except AmbiguousOutcome as exc:
            # Leave it in `executing`: the next attempt reconciles via lookup before calling again.
            delay = self._backoff(intent["attempts"] + 1)
            with tx(self.conn):
                self._set(intent["id"], last_error=str(exc), next_attempt_at=self.clock() + delay)
                self.audit.record(
                    "intent.outcome_unknown",
                    actor="engine",
                    run_id=intent["run_id"],
                    subject=intent["id"],
                    data={"error": str(exc)},
                )
            raise IntentRetry(self.get(intent["id"]), str(exc), delay) from exc  # type: ignore[arg-type]
        except PermanentError as exc:
            with tx(self.conn):
                self._set(intent["id"], status="failed", last_error=str(exc))
                self.audit.record(
                    "intent.failed",
                    actor="engine",
                    run_id=intent["run_id"],
                    subject=intent["id"],
                    data={"error": str(exc)},
                )
            raise IntentFailed(self.get(intent["id"]), str(exc), kind="tool_permanent") from exc  # type: ignore[arg-type]
        with tx(self.conn):
            self._set(intent["id"], status="executed", receipt_json=dumps(receipt.as_dict()), last_error=None)
            self.audit.record(
                "intent.executed",
                actor="engine",
                run_id=intent["run_id"],
                subject=intent["id"],
                data={"receipt": receipt.as_dict()},
            )
        return receipt

    def _verify(self, intent: dict[str, Any], adapter: Any) -> None:
        receipt = Receipt(**intent["receipt"])
        result: Verification | None = None
        for attempt in range(1, self.verify_attempts + 1):
            result = adapter.verify(intent["action"], receipt, intent["payload"])
            if result.ok:
                break
            if attempt < self.verify_attempts:
                time.sleep(min(0.05 * attempt, 0.2))  # destinations are often eventually consistent
        assert result is not None
        with tx(self.conn):
            if result.ok:
                self._set(intent["id"], status="verified", verification_json=dumps(result.as_dict()))
                self.audit.record(
                    "intent.verified",
                    actor="engine",
                    run_id=intent["run_id"],
                    subject=intent["id"],
                    data=result.as_dict(),
                )
                return
            message = "; ".join(result.mismatches)
            self._set(
                intent["id"],
                status="failed",
                verification_json=dumps(result.as_dict()),
                last_error=f"verification failed: {message}",
            )
            self.audit.record(
                "intent.verification_failed",
                actor="engine",
                run_id=intent["run_id"],
                subject=intent["id"],
                data=result.as_dict(),
            )
        raise IntentFailed(self.get(intent["id"]), f"verification failed: {message}", kind="verification_failed")  # type: ignore[arg-type]

    def _backoff(self, attempt: int) -> float:
        base = min(self.retry_max, self.retry_base * (2 ** (attempt - 1)))
        return base * (0.8 + 0.4 * random.random())

    # ---- manual recovery ---------------------------------------------------
    def reset_for_retry(self, intent_id: str, actor: str) -> None:
        """After an operator fixes the cause (e.g. restores a token), allow another attempt."""
        intent = self.get(intent_id)
        if intent is None or intent["status"] != "failed":
            return
        with tx(self.conn):
            # A failed verification may still have written something: reconcile instead of resending.
            status = "executing" if intent["receipt"] else "proposed"
            self._set(intent_id, status=status, last_error=None, next_attempt_at=None)
            self.audit.record(
                "intent.reset", actor=actor, run_id=intent["run_id"], subject=intent_id, data={"to": status}
            )
