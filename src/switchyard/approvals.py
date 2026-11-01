"""Approval requests and signed decisions.

An approval is bound to one intent *and the hash of its exact payload*. If an
agent later revises the email, the hash changes and the old approval no longer
counts. Decisions are HMAC-signed with a server-side secret, and the approver's
role is re-checked against the current policy at execution time.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .audit import AuditLog
from .canonical import canonical_json, dumps, loads, new_id
from .db import tx
from .policy import Classification, Policy


class ApprovalError(Exception):
    pass


CheckStatus = Literal["approved", "pending", "rejected", "expired", "missing", "invalid"]


@dataclass
class ApprovalCheck:
    status: CheckStatus
    reason: str
    approval: dict[str, Any] | None = None


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["required_roles"] = loads(out["required_roles"], [])
    out["preview"] = loads(out.pop("preview_json"), {})
    return out


class Approvals:
    def __init__(
        self,
        conn: sqlite3.Connection,
        secret: bytes,
        policy: Policy,
        audit: AuditLog,
        clock: Callable[[], float] = time.time,
    ):
        self.conn = conn
        self.secret = secret
        self.policy = policy
        self.audit = audit
        self.clock = clock

    def _sign(self, a: dict[str, Any], decision: str, decided_by: str, decided_at: float) -> str:
        message = canonical_json(
            [a["id"], a["intent_id"], a["payload_hash"], decision, decided_by, round(decided_at, 6)]
        )
        return hmac.new(self.secret, message.encode(), hashlib.sha256).hexdigest()

    # ---- requests ----------------------------------------------------------
    def request(
        self, intent: dict[str, Any], classification: Classification, preview: dict[str, Any]
    ) -> dict[str, Any]:
        with tx(self.conn):
            existing = self.conn.execute(
                "SELECT * FROM approvals WHERE intent_id = ? AND payload_hash = ? AND status IN ('pending','approved')"
                " ORDER BY requested_at DESC LIMIT 1",
                (intent["id"], intent["payload_hash"]),
            ).fetchone()
            if existing:
                return _row(existing)  # type: ignore[return-value]
            superseded = self.conn.execute(
                "UPDATE approvals SET status = 'superseded' WHERE intent_id = ? AND status IN ('pending','approved')",
                (intent["id"],),
            ).rowcount
            now = self.clock()
            approval_id = new_id("apr")
            self.conn.execute(
                "INSERT INTO approvals (id, intent_id, run_id, action, risk_class, payload_hash, required_roles,"
                " preview_json, status, requested_at, expires_at) VALUES (?,?,?,?,?,?,?,?, 'pending', ?, ?)",
                (
                    approval_id,
                    intent["id"],
                    intent["run_id"],
                    intent["action"],
                    classification.risk_class,
                    intent["payload_hash"],
                    dumps(list(classification.approver_roles)),
                    dumps(preview),
                    now,
                    now + classification.expires_hours * 3600,
                ),
            )
            self.audit.record(
                "approval.requested",
                actor="engine",
                run_id=intent["run_id"],
                subject=approval_id,
                data={
                    "intent_id": intent["id"],
                    "action": intent["action"],
                    "risk_class": classification.risk_class,
                    "reasons": list(classification.reasons),
                    "payload_hash": intent["payload_hash"],
                    "required_roles": list(classification.approver_roles),
                    "superseded_previous": superseded,
                },
            )
            return self.get(approval_id)  # type: ignore[return-value]

    def get(self, approval_id: str) -> dict[str, Any] | None:
        return _row(self.conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())

    def list(self, status: str | None = "pending", run_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM approvals WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        return [_row(r) for r in self.conn.execute(sql + " ORDER BY requested_at", args)]  # type: ignore[misc]

    # ---- decisions ---------------------------------------------------------
    def decide(
        self, approval_id: str, principal: str, decision: Literal["approve", "reject"], note: str | None = None
    ) -> dict[str, Any]:
        if decision not in ("approve", "reject"):
            raise ApprovalError("decision must be 'approve' or 'reject'")
        with tx(self.conn):
            approval = self.get(approval_id)
            if approval is None:
                raise ApprovalError(f"no approval {approval_id}")
            now = self.clock()
            if approval["status"] == "pending" and now > approval["expires_at"]:
                self._expire(approval)
                raise ApprovalError("approval request has expired; the workflow will raise a new one")
            if approval["status"] != "pending":
                raise ApprovalError(f"approval is already {approval['status']}")
            roles = self.policy.principal_roles(principal)
            if not roles & set(approval["required_roles"]):
                self.audit.record(
                    "approval.denied_unauthorised",
                    actor=principal,
                    run_id=approval["run_id"],
                    subject=approval_id,
                    data={"principal_roles": sorted(roles), "required_roles": approval["required_roles"]},
                )
                raise ApprovalError(f"{principal} lacks a required role {approval['required_roles']}")
            status = "approved" if decision == "approve" else "rejected"
            signature = self._sign(approval, status, principal, now)
            self.conn.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, note = ?, signature = ? WHERE id = ?",
                (status, principal, now, note, signature, approval_id),
            )
            self.audit.record(
                f"approval.{status}",
                actor=principal,
                run_id=approval["run_id"],
                subject=approval_id,
                data={"intent_id": approval["intent_id"], "payload_hash": approval["payload_hash"], "note": note},
            )
            return self.get(approval_id)  # type: ignore[return-value]

    def _expire(self, approval: dict[str, Any]) -> None:
        self.conn.execute("UPDATE approvals SET status = 'expired' WHERE id = ?", (approval["id"],))
        self.audit.record("approval.expired", actor="engine", run_id=approval["run_id"], subject=approval["id"])

    # ---- execution-time check ---------------------------------------------
    def check(self, intent: dict[str, Any]) -> ApprovalCheck:
        """Called by the executor immediately before a gated action runs."""
        approval = _row(
            self.conn.execute(
                "SELECT * FROM approvals WHERE intent_id = ? AND status != 'superseded' ORDER BY requested_at DESC LIMIT 1",
                (intent["id"],),
            ).fetchone()
        )
        if approval is None:
            return ApprovalCheck("missing", "no approval has been requested")
        if approval["payload_hash"] != intent["payload_hash"]:
            return ApprovalCheck("invalid", "payload changed after approval was requested", approval)
        status = approval["status"]
        if status == "pending":
            if self.clock() > approval["expires_at"]:
                with tx(self.conn):
                    self._expire(approval)
                return ApprovalCheck("expired", "nobody decided before the request expired", approval)
            return ApprovalCheck("pending", "waiting for a decision", approval)
        if status == "expired":
            return ApprovalCheck("expired", "nobody decided before the request expired", approval)
        expected = self._sign(approval, status, approval["decided_by"] or "", approval["decided_at"] or 0.0)
        if not hmac.compare_digest(expected, approval["signature"] or ""):
            return ApprovalCheck("invalid", "decision signature does not verify", approval)
        if status == "rejected":
            return ApprovalCheck("rejected", f"rejected by {approval['decided_by']}", approval)
        if not self.policy.principal_roles(approval["decided_by"]) & set(approval["required_roles"]):
            return ApprovalCheck("invalid", f"{approval['decided_by']} no longer holds a required role", approval)
        return ApprovalCheck("approved", f"approved by {approval['decided_by']}", approval)
