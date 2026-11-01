"""Adapters for the simulated apps.

Each adapter uses the same idempotency technique the matching real API offers
(documented in docs/INTEGRATIONS.md), so swapping in the real client keeps the
engine's guarantees.
"""

from __future__ import annotations

import base64
from typing import Any

from ..canonical import sha256_hex
from ..sandbox import SURVEY_CALENDAR, Conflict, SandboxApps
from .base import PermanentError, Receipt, Verification, compare_fields

SENDER = "proposals@demo-energy.test"


def message_id_for(idem_key: str) -> str:
    return f"<{idem_key}@switchyard.demo-energy.test>"


def calendar_event_id_for(idem_key: str) -> str:
    # Google Calendar ids allow only base32hex characters (a-v, 0-9).
    return base64.b32hexencode(idem_key.encode()).decode().lower().rstrip("=")


def _missing(what: str) -> Verification:
    return Verification(ok=False, mismatches=[f"{what} not found at the destination"])


class CrmAdapter:
    """HubSpot-style CRM: upsert by email; deals carry a unique `switchyard_key` property."""

    app = "crm"

    def __init__(self, apps: SandboxApps):
        self.apps = apps

    def execute(self, action: str, payload: dict[str, Any], idem_key: str) -> Receipt:
        if action == "crm.upsert_contact":
            out = self.apps.crm_upsert_contact(
                payload["email"],
                payload.get("name"),
                payload.get("company"),
                payload.get("phone"),
                payload.get("segment"),
            )
            return Receipt(self.app, out["id"], {"created": out["created"]})
        if action == "crm.create_deal":
            try:
                out = self.apps.crm_create_deal(
                    payload["contact_id"],
                    payload["title"],
                    payload.get("stage", "new"),
                    payload.get("amount"),
                    payload.get("currency"),
                    payload.get("owner"),
                    idem_key,
                )
            except Conflict as exc:  # an earlier attempt already created it
                return Receipt(self.app, exc.existing_id, {"adopted_on_conflict": True})
            return Receipt(self.app, out["id"])
        if action == "crm.update_deal_stage":
            out = self.apps.crm_update_deal_stage(payload["deal_id"], payload["stage"], payload.get("amount"))
            return Receipt(self.app, out["id"])
        if action == "crm.add_note":
            existing = self.lookup(action, idem_key, payload)
            if existing:
                return existing
            out = self.apps.crm_add_note(payload["object_id"], payload["body"], payload.get("author"), idem_key)
            return Receipt(self.app, out["id"])
        raise PermanentError(f"crm adapter does not support {action}")

    def lookup(self, action: str, idem_key: str, payload: dict[str, Any]) -> Receipt | None:
        if action == "crm.create_deal":
            deal = self.apps.crm_find_deal_by_key(idem_key)
            return Receipt(self.app, deal["id"], {"found_by": "switchyard_key"}) if deal else None
        if action == "crm.add_note":
            notes = self.apps.crm_notes(idem_key=idem_key)
            return Receipt(self.app, notes[0]["id"], {"found_by": "switchyard_key"}) if notes else None
        # upsert_contact and update_deal_stage are naturally idempotent: re-running is safe.
        return None

    def verify(self, action: str, receipt: Receipt, payload: dict[str, Any]) -> Verification:
        if action == "crm.upsert_contact":
            contact = self.apps.crm_get_contact(payload["email"])
            if not contact or contact["id"] != receipt.external_id:
                return _missing(f"contact {receipt.external_id}")
            return compare_fields(payload, contact, [f for f in ("name", "company") if payload.get(f)])
        if action in ("crm.create_deal", "crm.update_deal_stage"):
            deal = self.apps.crm_get_deal(receipt.external_id)
            if not deal:
                return _missing(f"deal {receipt.external_id}")
            fields = ["contact_id", "title"] if action == "crm.create_deal" else ["stage"]
            return compare_fields(payload, deal, fields)
        if action == "crm.add_note":
            notes = [n for n in self.apps.crm_notes(object_id=payload["object_id"]) if n["id"] == receipt.external_id]
            if not notes:
                return _missing(f"note {receipt.external_id}")
            return compare_fields(payload, notes[0], ["body"])
        return Verification(ok=False, mismatches=[f"no verifier for {action}"])


class EmailAdapter:
    """Gmail-style send. SMTP has no idempotency, so the key travels as the Message-ID header
    and lookup searches the Sent folder for it (Gmail query `rfc822msgid:`)."""

    app = "email"

    def __init__(self, apps: SandboxApps):
        self.apps = apps

    def execute(self, action: str, payload: dict[str, Any], idem_key: str) -> Receipt:
        if action != "email.send":
            raise PermanentError(f"email adapter does not support {action}")
        headers = {"Message-ID": message_id_for(idem_key), "X-Switchyard-Key": idem_key}
        out = self.apps.email_send(
            payload.get("from", SENDER),
            payload["to"],
            payload.get("cc", []),
            payload["subject"],
            payload["body"],
            headers,
        )
        return Receipt(self.app, out["id"], {"message_id": out["message_id"]})

    def lookup(self, action: str, idem_key: str, payload: dict[str, Any]) -> Receipt | None:
        found = self.apps.email_search(message_id=message_id_for(idem_key))
        return (
            Receipt(self.app, found[0]["id"], {"message_id": found[0]["message_id"], "found_by": "Message-ID"})
            if found
            else None
        )

    def verify(self, action: str, receipt: Receipt, payload: dict[str, Any]) -> Verification:
        found = self.apps.email_search(message_id=receipt.details.get("message_id"))
        if not found:
            return _missing(f"sent message {receipt.details.get('message_id')}")
        if len(found) > 1:
            return Verification(
                ok=False, checked={"copies": len(found)}, mismatches=[f"{len(found)} copies were delivered"]
            )
        sent = found[0]
        result = compare_fields({"to": payload["to"], "subject": payload["subject"]}, sent, ["to", "subject"])
        if sha256_hex(sent["body"]) != sha256_hex(payload["body"]):
            result.ok = False
            result.mismatches.append("body differs from the approved text")
        result.checked["body_sha256"] = sha256_hex(sent["body"])[:16]
        return result


class DocsAdapter:
    """Drive-style docs: the key is stored in appProperties and found with an appProperties query."""

    app = "docs"

    def __init__(self, apps: SandboxApps):
        self.apps = apps

    def execute(self, action: str, payload: dict[str, Any], idem_key: str) -> Receipt:
        if action != "docs.create":
            raise PermanentError(f"docs adapter does not support {action}")
        existing = self.lookup(action, idem_key, payload)
        if existing:
            return existing
        out = self.apps.docs_create(
            payload["title"],
            payload.get("folder", "Proposals"),
            payload["body"],
            payload.get("shared_with", []),
            {"switchyard_key": idem_key},
        )
        return Receipt(self.app, out["id"], {"url": out["url"]})

    def lookup(self, action: str, idem_key: str, payload: dict[str, Any]) -> Receipt | None:
        found = self.apps.docs_find_by_property("switchyard_key", idem_key)
        return (
            Receipt(self.app, found[0]["id"], {"url": found[0]["url"], "found_by": "appProperties"}) if found else None
        )

    def verify(self, action: str, receipt: Receipt, payload: dict[str, Any]) -> Verification:
        doc = self.apps.docs_get(receipt.external_id)
        if not doc:
            return _missing(f"document {receipt.external_id}")
        result = compare_fields(payload, doc, ["title"])
        if sha256_hex(doc["body"]) != sha256_hex(payload["body"]):
            result.ok = False
            result.mismatches.append("document body differs from the reviewed draft")
        return result


class CalendarAdapter:
    """Google-Calendar-style: the event id is derived from the key, so a retry gets 409, not a twin."""

    app = "calendar"

    def __init__(self, apps: SandboxApps):
        self.apps = apps

    def execute(self, action: str, payload: dict[str, Any], idem_key: str) -> Receipt:
        if action != "calendar.create_event":
            raise PermanentError(f"calendar adapter does not support {action}")
        event_id = calendar_event_id_for(idem_key)
        try:
            self.apps.calendar_insert(
                payload.get("calendar", SURVEY_CALENDAR),
                event_id,
                payload["title"],
                payload["start"],
                payload["end"],
                payload.get("attendees", []),
                payload.get("description", ""),
            )
        except Conflict:
            return Receipt(self.app, event_id, {"adopted_on_conflict": True})
        return Receipt(self.app, event_id)

    def lookup(self, action: str, idem_key: str, payload: dict[str, Any]) -> Receipt | None:
        event_id = calendar_event_id_for(idem_key)
        return Receipt(self.app, event_id, {"found_by": "event id"}) if self.apps.calendar_get(event_id) else None

    def verify(self, action: str, receipt: Receipt, payload: dict[str, Any]) -> Verification:
        event = self.apps.calendar_get(receipt.external_id)
        if not event:
            return _missing(f"event {receipt.external_id}")
        return compare_fields(payload, event, ["title", "start", "end"])


class ChatAdapter:
    """Slack-style: the key rides in message metadata; lookup scans recent channel history."""

    app = "chat"

    def __init__(self, apps: SandboxApps):
        self.apps = apps

    def execute(self, action: str, payload: dict[str, Any], idem_key: str) -> Receipt:
        if action != "chat.post":
            raise PermanentError(f"chat adapter does not support {action}")
        existing = self.lookup(action, idem_key, payload)
        if existing:
            return existing
        metadata = {"event_type": "switchyard_action", "event_payload": {"idempotency_key": idem_key}}
        out = self.apps.chat_post(payload["channel"], payload["text"], metadata)
        return Receipt(self.app, out["ts"], {"channel": out["channel"]})

    def lookup(self, action: str, idem_key: str, payload: dict[str, Any]) -> Receipt | None:
        for message in self.apps.chat_history(payload.get("channel")):
            if message["metadata"].get("event_payload", {}).get("idempotency_key") == idem_key:
                return Receipt(self.app, message["ts"], {"channel": message["channel"], "found_by": "metadata"})
        return None

    def verify(self, action: str, receipt: Receipt, payload: dict[str, Any]) -> Verification:
        found = [m for m in self.apps.chat_history(payload["channel"]) if m["ts"] == receipt.external_id]
        if not found:
            return _missing(f"message {receipt.external_id}")
        return compare_fields(payload, found[0], ["text"])


class BillingAdapter:
    app = "billing"

    def __init__(self, apps: SandboxApps):
        self.apps = apps

    def execute(self, action: str, payload: dict[str, Any], idem_key: str) -> Receipt:
        if action != "billing.issue_invoice":
            raise PermanentError(f"billing adapter does not support {action}")
        try:
            out = self.apps.billing_issue_invoice(
                payload["customer_email"], payload["amount"], payload["currency"], payload.get("lines", []), idem_key
            )
        except Conflict as exc:
            return Receipt(self.app, exc.existing_id, {"adopted_on_conflict": True})
        return Receipt(self.app, out["id"])

    def lookup(self, action: str, idem_key: str, payload: dict[str, Any]) -> Receipt | None:
        invoice = self.apps.billing_find_by_key(idem_key)
        return Receipt(self.app, invoice["id"], {"found_by": "idempotency key"}) if invoice else None

    def verify(self, action: str, receipt: Receipt, payload: dict[str, Any]) -> Verification:
        invoice = self.apps.billing_get(receipt.external_id)
        if not invoice:
            return _missing(f"invoice {receipt.external_id}")
        return compare_fields(payload, invoice, ["customer_email", "amount", "currency"])


def sandbox_adapters(apps: SandboxApps) -> list[Any]:
    return [
        CrmAdapter(apps),
        EmailAdapter(apps),
        DocsAdapter(apps),
        CalendarAdapter(apps),
        ChatAdapter(apps),
        BillingAdapter(apps),
    ]
