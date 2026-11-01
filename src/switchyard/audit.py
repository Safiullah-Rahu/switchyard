"""Append-only, hash-chained audit log.

Every state change (step claimed, intent executed, approval decided, exception
raised …) is written here in the same transaction as the change itself. Each
row carries the hash of the previous row, so editing or deleting history is
detectable with `switchyard audit-verify`. In production the chain head is
also shipped to write-once storage on a schedule.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_json, dumps, loads, sha256_hex
from .db import tx

GENESIS = "0" * 64


def _entry_hash(prev_hash: str, entry: dict[str, Any]) -> str:
    return sha256_hex(prev_hash + canonical_json(entry))


@dataclass
class ChainCheck:
    ok: bool
    checked: int
    first_bad_seq: int | None = None
    reason: str = ""


class AuditLog:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    def record(
        self,
        event: str,
        *,
        actor: str,
        run_id: str | None = None,
        subject: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        with tx(self.conn):
            last = self.conn.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (last["seq"] + 1) if last else 1
            prev = last["hash"] if last else GENESIS
            entry = {
                "seq": seq,
                "ts": round(self.clock(), 6),
                "actor": actor,
                "event": event,
                "run_id": run_id,
                "subject": subject,
                "data": data or {},
            }
            digest = _entry_hash(prev, entry)
            self.conn.execute(
                "INSERT INTO audit (seq, ts, actor, event, run_id, subject, data_json, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (seq, entry["ts"], actor, event, run_id, subject, dumps(entry["data"]), prev, digest),
            )
            return seq

    def verify(self) -> ChainCheck:
        prev = GENESIS
        count = 0
        for row in self.conn.execute("SELECT * FROM audit ORDER BY seq"):
            count += 1
            entry = {
                "seq": row["seq"],
                "ts": row["ts"],
                "actor": row["actor"],
                "event": row["event"],
                "run_id": row["run_id"],
                "subject": row["subject"],
                "data": loads(row["data_json"], {}),
            }
            if row["prev_hash"] != prev:
                return ChainCheck(False, count, row["seq"], "prev_hash does not match the row before")
            if _entry_hash(prev, entry) != row["hash"]:
                return ChainCheck(False, count, row["seq"], "row content does not match its hash")
            prev = row["hash"]
        return ChainCheck(True, count)

    def entries(self, run_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if run_id:
            rows = self.conn.execute("SELECT * FROM audit WHERE run_id = ? ORDER BY seq LIMIT ?", (run_id, limit))
        else:
            rows = self.conn.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        return [
            {
                "seq": r["seq"],
                "ts": r["ts"],
                "actor": r["actor"],
                "event": r["event"],
                "run_id": r["run_id"],
                "subject": r["subject"],
                "data": loads(r["data_json"], {}),
                "hash": r["hash"],
            }
            for r in rows
        ]
