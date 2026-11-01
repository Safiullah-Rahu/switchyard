"""Simulated business applications (CRM, email, docs, calendar, chat, billing).

These stand in for HubSpot / Gmail / Google Drive / Google Calendar / Slack /
an invoicing tool so the whole system runs offline. They live in their own
database file, so from Switchyard's point of view they are external systems.

Each one copies the idempotency behaviour of the real API it imitates — and,
just as importantly, the lack of it: like SMTP, the email app happily sends
the same message twice. The `faults` table lets demos and tests make any
operation fail in a specific way (see `FaultMode`).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from .adapters.base import AmbiguousOutcome, PermanentError, SimulatedCrash, TransientError
from .canonical import new_id

FaultMode = Literal["transient", "permanent", "timeout_after_write", "drop", "crash_after_write"]
FAULT_MODES: tuple[str, ...] = ("transient", "permanent", "timeout_after_write", "drop", "crash_after_write")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT, company TEXT, phone TEXT,
    segment TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS deals (
    id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, title TEXT NOT NULL, stage TEXT NOT NULL,
    amount REAL, currency TEXT, owner TEXT, idem_key TEXT UNIQUE, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY, object_id TEXT NOT NULL, body TEXT NOT NULL, author TEXT,
    idem_key TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL, sender TEXT NOT NULL, to_json TEXT NOT NULL,
    cc_json TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, headers_json TEXT NOT NULL, sent_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, folder TEXT NOT NULL, body TEXT NOT NULL,
    shared_with_json TEXT NOT NULL, app_properties_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, calendar TEXT NOT NULL, title TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
    attendees_json TEXT NOT NULL, description TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chat_messages (
    ts TEXT PRIMARY KEY, channel TEXT NOT NULL, text TEXT NOT NULL, metadata_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY, customer_email TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
    lines_json TEXT NOT NULL, idem_key TEXT UNIQUE, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS faults (
    app TEXT NOT NULL, action TEXT NOT NULL, mode TEXT NOT NULL, remaining INTEGER NOT NULL,
    PRIMARY KEY (app, action, mode));
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, app TEXT NOT NULL, action TEXT NOT NULL, outcome TEXT NOT NULL, at REAL NOT NULL);
"""

TEMPLATES = {
    "commercial-solar-proposal": {
        "name": "commercial-solar-proposal",
        "sections": [
            "Summary",
            "Your site and energy use",
            "Proposed system",
            "Estimated performance and savings",
            "Next steps",
        ],
        "required_disclaimer": "All figures are estimates and are subject to a site survey.",
        "tone": "Plain, factual, no superlatives. Never promise savings.",
    },
    "residential-solar-proposal": {
        "name": "residential-solar-proposal",
        "sections": ["Summary", "Your home", "Proposed system", "Estimated savings", "Next steps"],
        "required_disclaimer": "All figures are estimates and are subject to a site survey.",
        "tone": "Friendly and plain. Never promise savings.",
    },
}

SURVEY_CALENDAR = "surveys@demo-energy.test"


class Conflict(Exception):
    """The destination already holds an object with this unique key (HTTP 409)."""

    def __init__(self, existing_id: str):
        super().__init__(f"409 Conflict: already exists as {existing_id}")
        self.existing_id = existing_id


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class SandboxApps:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- fault injection ---------------------------------------------------
    def set_fault(self, app: str, action: str, mode: FaultMode, times: int = 1) -> None:
        if mode not in FAULT_MODES:
            raise ValueError(f"unknown fault mode {mode}")
        self.conn.execute(
            "INSERT INTO faults (app, action, mode, remaining) VALUES (?,?,?,?)"
            " ON CONFLICT(app, action, mode) DO UPDATE SET remaining = excluded.remaining",
            (app, action, mode, times),
        )

    def clear_faults(self) -> None:
        self.conn.execute("DELETE FROM faults")

    def _take_fault(self, app: str, action: str) -> str | None:
        row = self.conn.execute(
            "SELECT mode, remaining FROM faults WHERE app = ? AND action = ? AND remaining > 0 LIMIT 1",
            (app, action),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            "UPDATE faults SET remaining = remaining - 1 WHERE app = ? AND action = ? AND mode = ?",
            (app, action, row["mode"]),
        )
        return row["mode"]

    def _log(self, app: str, action: str, outcome: str) -> None:
        self.conn.execute(
            "INSERT INTO calls (app, action, outcome, at) VALUES (?,?,?,?)", (app, action, outcome, self.clock())
        )

    def _write(self, app: str, action: str, apply: Callable[[], Any], fake: Callable[[], Any]) -> Any:
        """Run one write with whatever fault is armed for (app, action)."""
        mode = self._take_fault(app, action)
        if mode == "transient":
            self._log(app, action, "503")
            raise TransientError(f"{app}: 503 Service Unavailable")
        if mode == "permanent":
            self._log(app, action, "403")
            raise PermanentError(f"{app}: 403 Forbidden — token lacks the required scope")
        if mode == "drop":
            self._log(app, action, "200-dropped")
            return fake()
        result = apply()
        if mode == "timeout_after_write":
            self._log(app, action, "applied-then-timeout")
            raise AmbiguousOutcome(f"{app}: read timeout after request was sent")
        if mode == "crash_after_write":
            self._log(app, action, "applied-then-crash")
            if os.environ.get("SWITCHYARD_HARD_CRASH") == "1":
                os._exit(137)
            raise SimulatedCrash(f"process died after {app}.{action} was applied")
        self._log(app, action, "200")
        return result

    # ---- CRM ---------------------------------------------------------------
    def crm_upsert_contact(
        self, email: str, name: str | None, company: str | None, phone: str | None, segment: str | None
    ) -> dict[str, Any]:
        now = self.clock()

        def apply() -> dict[str, Any]:
            existing = self.crm_get_contact(email)
            if existing:
                self.conn.execute(
                    "UPDATE contacts SET name = COALESCE(?, name), company = COALESCE(?, company),"
                    " phone = COALESCE(?, phone), segment = COALESCE(?, segment), updated_at = ? WHERE id = ?",
                    (name, company, phone, segment, now, existing["id"]),
                )
                return {"id": existing["id"], "created": False}
            contact_id = new_id("ct")
            self.conn.execute(
                "INSERT INTO contacts (id, email, name, company, phone, segment, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (contact_id, email.lower(), name, company, phone, segment, now, now),
            )
            return {"id": contact_id, "created": True}

        return self._write("crm", "upsert_contact", apply, lambda: {"id": new_id("ct"), "created": True})

    def crm_create_deal(
        self,
        contact_id: str,
        title: str,
        stage: str,
        amount: float | None,
        currency: str | None,
        owner: str | None,
        idem_key: str | None,
    ) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            if idem_key:
                row = self.conn.execute("SELECT id FROM deals WHERE idem_key = ?", (idem_key,)).fetchone()
                if row:
                    raise Conflict(row["id"])
            deal_id = new_id("dl")
            now = self.clock()
            self.conn.execute(
                "INSERT INTO deals (id, contact_id, title, stage, amount, currency, owner, idem_key, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (deal_id, contact_id, title, stage, amount, currency, owner, idem_key, now, now),
            )
            return {"id": deal_id}

        return self._write("crm", "create_deal", apply, lambda: {"id": new_id("dl")})

    def crm_update_deal_stage(self, deal_id: str, stage: str, amount: float | None = None) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            changed = self.conn.execute(
                "UPDATE deals SET stage = ?, amount = COALESCE(?, amount), updated_at = ? WHERE id = ?",
                (stage, amount, self.clock(), deal_id),
            ).rowcount
            if not changed:
                raise PermanentError(f"crm: 404 deal {deal_id} not found")
            return {"id": deal_id}

        return self._write("crm", "update_deal_stage", apply, lambda: {"id": deal_id})

    def crm_add_note(self, object_id: str, body: str, author: str | None, idem_key: str | None) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            note_id = new_id("nt")
            self.conn.execute(
                "INSERT INTO notes (id, object_id, body, author, idem_key, created_at) VALUES (?,?,?,?,?,?)",
                (note_id, object_id, body, author, idem_key, self.clock()),
            )
            return {"id": note_id}

        return self._write("crm", "add_note", apply, lambda: {"id": new_id("nt")})

    def crm_get_contact(self, email: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM contacts WHERE email = ?", (email.lower(),)).fetchone()
        return dict(row) if row else None

    def crm_get_contact_by_id(self, contact_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return dict(row) if row else None

    def crm_search_contacts(self, query: str) -> list[dict[str, Any]]:
        like = f"%{query.lower()}%"
        rows = self.conn.execute(
            "SELECT * FROM contacts WHERE lower(email) LIKE ? OR lower(name) LIKE ? OR lower(company) LIKE ? ORDER BY created_at",
            (like, like, like),
        )
        return [dict(r) for r in rows]

    def crm_get_deal(self, deal_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        return dict(row) if row else None

    def crm_find_deal_by_key(self, idem_key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM deals WHERE idem_key = ?", (idem_key,)).fetchone()
        return dict(row) if row else None

    def crm_deals_for_email(self, email: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT d.* FROM deals d JOIN contacts c ON c.id = d.contact_id WHERE c.email = ? ORDER BY d.created_at",
            (email.lower(),),
        )
        return [dict(r) for r in rows]

    def crm_notes(self, object_id: str | None = None, idem_key: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM notes WHERE 1=1", []
        if object_id:
            sql += " AND object_id = ?"
            args.append(object_id)
        if idem_key:
            sql += " AND idem_key = ?"
            args.append(idem_key)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY created_at", args)]

    # ---- Email (no native deduplication, like SMTP) -------------------------
    def email_send(
        self, sender: str, to: list[str], cc: list[str], subject: str, body: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        message_id = headers.get("Message-ID") or f"<{new_id('m')}@mail.demo-energy.test>"

        def apply() -> dict[str, Any]:
            email_id = new_id("em")
            self.conn.execute(
                "INSERT INTO emails (id, message_id, sender, to_json, cc_json, subject, body, headers_json, sent_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (email_id, message_id, sender, _j(to), _j(cc), subject, body, _j(headers), self.clock()),
            )
            return {"id": email_id, "message_id": message_id}

        return self._write("email", "send", apply, lambda: {"id": new_id("em"), "message_id": message_id})

    def email_search(self, message_id: str | None = None, to: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM emails WHERE 1=1", []
        if message_id:
            sql += " AND message_id = ?"
            args.append(message_id)
        if to:
            sql += " AND to_json LIKE ?"
            args.append(f'%"{to.lower()}"%')
        out = []
        for r in self.conn.execute(sql + " ORDER BY sent_at", args):
            item = dict(r)
            item["to"] = json.loads(item.pop("to_json"))
            item["cc"] = json.loads(item.pop("cc_json"))
            item["headers"] = json.loads(item.pop("headers_json"))
            out.append(item)
        return out

    # ---- Documents (Drive-like: lookup by appProperties) --------------------
    def docs_create(
        self, title: str, folder: str, body: str, shared_with: list[str], app_properties: dict[str, str]
    ) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            doc_id = new_id("doc")
            self.conn.execute(
                "INSERT INTO documents (id, title, folder, body, shared_with_json, app_properties_json, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (doc_id, title, folder, body, _j(shared_with), _j(app_properties), self.clock()),
            )
            return {"id": doc_id, "url": f"https://drive.demo-energy.test/d/{doc_id}"}

        fake_id = new_id("doc")
        return self._write(
            "docs", "create", apply, lambda: {"id": fake_id, "url": f"https://drive.demo-energy.test/d/{fake_id}"}
        )

    def docs_get(self, doc_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["shared_with"] = json.loads(item.pop("shared_with_json"))
        item["app_properties"] = json.loads(item.pop("app_properties_json"))
        item["url"] = f"https://drive.demo-energy.test/d/{item['id']}"
        return item

    def docs_find_by_property(self, key: str, value: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT id, app_properties_json FROM documents ORDER BY created_at")
        return [self.docs_get(r["id"]) for r in rows if json.loads(r["app_properties_json"]).get(key) == value]  # type: ignore[misc]

    def docs_template(self, name: str) -> dict[str, Any] | None:
        return TEMPLATES.get(name)

    # ---- Calendar (Google-like: client-supplied event ids) -----------------
    def calendar_insert(
        self, calendar: str, event_id: str, title: str, start: str, end: str, attendees: list[str], description: str
    ) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            if self.calendar_get(event_id):
                raise Conflict(event_id)
            self.conn.execute(
                "INSERT INTO events (id, calendar, title, start, end, attendees_json, description, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (event_id, calendar, title, start, end, _j(attendees), description, self.clock()),
            )
            return {"id": event_id}

        return self._write("calendar", "create_event", apply, lambda: {"id": event_id})

    def calendar_get(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["attendees"] = json.loads(item.pop("attendees_json"))
        return item

    def calendar_events(self, calendar: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT id FROM events WHERE calendar = ? ORDER BY start", (calendar,))
        return [self.calendar_get(r["id"]) for r in rows]  # type: ignore[misc]

    def calendar_free_slots(
        self, calendar: str, start_date: str, days: int = 7, duration_minutes: int = 90
    ) -> list[dict[str, str]]:
        busy = [(e["start"], e["end"]) for e in self.calendar_events(calendar)]
        day = datetime.fromisoformat(start_date).replace(hour=0, minute=0, second=0, microsecond=0)
        slots: list[dict[str, str]] = []
        for _ in range(days):
            if day.weekday() < 5:
                for hour, minute in ((8, 30), (11, 0), (14, 0)):
                    s = day.replace(hour=hour, minute=minute)
                    e = s + timedelta(minutes=duration_minutes)
                    if not any(s.isoformat() < b_end and e.isoformat() > b_start for b_start, b_end in busy):
                        slots.append(
                            {
                                "start": s.isoformat(timespec="minutes"),
                                "end": e.isoformat(timespec="minutes"),
                                "weekday": s.strftime("%A"),
                            }
                        )
            day += timedelta(days=1)
        return slots

    # ---- Chat (Slack-like: message metadata) -------------------------------
    def chat_post(self, channel: str, text: str, metadata: dict[str, Any]) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            ts = f"{self.clock():.6f}"
            while self.conn.execute("SELECT 1 FROM chat_messages WHERE ts = ?", (ts,)).fetchone():
                ts = f"{float(ts) + 0.000001:.6f}"
            self.conn.execute(
                "INSERT INTO chat_messages (ts, channel, text, metadata_json) VALUES (?,?,?,?)",
                (ts, channel, text, _j(metadata)),
            )
            return {"ts": ts, "channel": channel}

        return self._write("chat", "post", apply, lambda: {"ts": f"{self.clock():.6f}", "channel": channel})

    def chat_history(self, channel: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM chat_messages", []
        if channel:
            sql += " WHERE channel = ?"
            args.append(channel)
        out = []
        for r in self.conn.execute(sql + " ORDER BY ts", args):
            item = dict(r)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            out.append(item)
        return out

    # ---- Billing -------------------------------------------------------------
    def billing_issue_invoice(
        self, customer_email: str, amount: float, currency: str, lines: list[dict[str, Any]], idem_key: str | None
    ) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            if idem_key:
                row = self.conn.execute("SELECT id FROM invoices WHERE idem_key = ?", (idem_key,)).fetchone()
                if row:
                    raise Conflict(row["id"])
            invoice_id = new_id("inv")
            self.conn.execute(
                "INSERT INTO invoices (id, customer_email, amount, currency, lines_json, idem_key, status, created_at)"
                " VALUES (?,?,?,?,?,?, 'issued', ?)",
                (invoice_id, customer_email, amount, currency, _j(lines), idem_key, self.clock()),
            )
            return {"id": invoice_id}

        return self._write("billing", "issue_invoice", apply, lambda: {"id": new_id("inv")})

    def billing_get(self, invoice_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return dict(row) if row else None

    def billing_find_by_key(self, idem_key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM invoices WHERE idem_key = ?", (idem_key,)).fetchone()
        return dict(row) if row else None

    # ---- Inspection ----------------------------------------------------------
    def counts(self) -> dict[str, int]:
        tables = ["contacts", "deals", "notes", "emails", "documents", "events", "chat_messages", "invoices"]
        return {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

    def call_log(self, app: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM calls", []
        if app:
            sql += " WHERE app = ?"
            args.append(app)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    # ---- Seed data ---------------------------------------------------------
    def seed(self, today: datetime | None = None) -> None:
        """Existing records a real CRM and calendar would already hold."""
        if self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]:
            return
        now = self.clock()
        self.conn.execute(
            "INSERT INTO contacts (id, email, name, company, phone, segment, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "ct_seed_rivera",
                "j.rivera@harbourcold.example.com",
                "Jo Rivera",
                "Harbour Cold Storage",
                "+44 1632 960111",
                "commercial",
                now,
                now,
            ),
        )
        self.conn.execute(
            "INSERT INTO deals (id, contact_id, title, stage, amount, currency, owner, idem_key, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "dl_seed_rivera",
                "ct_seed_rivera",
                "Harbour Cold Storage — rooftop PV",
                "qualified",
                96000,
                "GBP",
                "maya.patel",
                None,
                now,
                now,
            ),
        )
        base = (today or datetime.fromtimestamp(now)).replace(hour=0, minute=0, second=0, microsecond=0)
        for offset, hour in ((1, 8), (2, 11), (3, 14)):
            s = (base + timedelta(days=offset)).replace(hour=hour, minute=0)
            self.conn.execute(
                "INSERT INTO events (id, calendar, title, start, end, attendees_json, description, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"seedbusy{offset}",
                    SURVEY_CALENDAR,
                    "Survey (booked)",
                    s.isoformat(timespec="minutes"),
                    (s + timedelta(hours=3)).isoformat(timespec="minutes"),
                    "[]",
                    "existing booking",
                    now,
                ),
            )
