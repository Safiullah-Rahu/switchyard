"""SQLite schema and transaction helper.

SQLite in WAL mode is the smallest backend that gives durable, transactional
state for a single-site pilot. Every statement here is plain SQL that ports to
Postgres; see docs/ARCHITECTURE.md for the migration notes.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    workflow TEXT NOT NULL,
    version INTEGER NOT NULL,
    dedup_key TEXT UNIQUE,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    parent_run_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    status TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    lease_owner TEXT,
    lease_token INTEGER NOT NULL DEFAULT 0,
    lease_expires_at REAL,
    next_attempt_at REAL,
    waiting_on TEXT,
    output_json TEXT,
    notes_json TEXT NOT NULL DEFAULT '[]',
    revisions INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    started_at REAL,
    finished_at REAL,
    PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY,
    idem_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    slot TEXT NOT NULL,
    origin TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    action TEXT NOT NULL,
    app TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    risk_class TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL,
    receipt_json TEXT,
    verification_json TEXT,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES intents(id),
    run_id TEXT NOT NULL,
    action TEXT NOT NULL,
    risk_class TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    required_roles TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_by TEXT,
    decided_at REAL,
    note TEXT,
    signature TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    key TEXT NOT NULL,
    question TEXT NOT NULL,
    options_json TEXT NOT NULL,
    status TEXT NOT NULL,
    choice TEXT,
    decided_by TEXT,
    decided_at REAL,
    created_at REAL NOT NULL,
    UNIQUE (run_id, step, key)
);

CREATE TABLE IF NOT EXISTS exceptions (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    step TEXT,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    suggested_action TEXT,
    status TEXT NOT NULL,
    dedup_key TEXT UNIQUE,
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    step TEXT,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    requests INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    estimated INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_events (
    source TEXT NOT NULL,
    event_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    run_id TEXT,
    received_at REAL NOT NULL,
    PRIMARY KEY (source, event_id)
);

CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    run_id TEXT,
    subject TEXT,
    data_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_steps_status ON steps(status);
CREATE INDEX IF NOT EXISTS ix_intents_run ON intents(run_id);
CREATE INDEX IF NOT EXISTS ix_intents_status ON intents(status);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS ix_exceptions_status ON exceptions(status);
CREATE INDEX IF NOT EXISTS ix_audit_run ON audit(run_id);
CREATE INDEX IF NOT EXISTS ix_usage_run ON usage(run_id);
"""


class LockedConnection(sqlite3.Connection):
    """A connection that carries the re-entrant lock `tx()` uses to serialise transactions."""

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.tx_lock = threading.RLock()


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=False, timeout=10, factory=LockedConnection
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE … COMMIT. Nested calls on the same thread join the outer transaction;
    other threads sharing the connection wait for it to finish."""
    lock = getattr(conn, "tx_lock", None) or threading.RLock()
    with lock:
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
