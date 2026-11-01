"""HubSpot CRM adapter (CRM v3 objects API).

Idempotency strategy:
* contacts — batch upsert keyed on `email` (naturally idempotent);
* deals and notes — a custom property `switchyard_key` created with
  "unique value" switched on, so a retried create is rejected instead of
  duplicated, and the existing record is found with the search API.

Verification reads the object back **by id** (GET), not via search: HubSpot's
search index lags writes by a few seconds, so search is only used to *find* a
record after a conflict, never to prove a write.

Status: contract-tested against mocked HTTP (tests/test_http_adapters.py).
Run against a HubSpot developer test account before go-live.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .base import PermanentError, Receipt, Verification, compare_fields
from .http import fail_permanently, request

KEY_PROPERTY = "switchyard_key"
DEAL_TO_CONTACT = 3  # HubSpot-defined association type ids
NOTE_TO_DEAL = 214


class HubSpotCrmAdapter:
    app = "crm"

    def __init__(
        self,
        token: str,
        *,
        stage_ids: dict[str, str],
        pipeline: str = "default",
        base_url: str = "https://api.hubapi.com",
        client: httpx.Client | None = None,
    ):
        self.stage_ids = stage_ids
        self.pipeline = pipeline
        self.client = client or httpx.Client(base_url=base_url, timeout=httpx.Timeout(10.0, connect=5.0))
        self.client.headers["Authorization"] = f"Bearer {token}"

    # ---- execute ---------------------------------------------------------------------
    def execute(self, action: str, payload: dict[str, Any], idem_key: str) -> Receipt:
        if action == "crm.upsert_contact":
            props = {
                "email": payload["email"],
                "firstname": payload.get("name"),
                "company": payload.get("company"),
                "phone": payload.get("phone"),
            }
            body = {
                "inputs": [
                    {"idProperty": "email", "id": payload["email"], "properties": {k: v for k, v in props.items() if v}}
                ]
            }
            r = request(self.client, "POST", "/crm/v3/objects/contacts/batch/upsert", json=body)
            if r.status_code not in (200, 201, 207):
                raise fail_permanently(r, "contact upsert")
            result = r.json()["results"][0]
            return Receipt(self.app, str(result["id"]), {"created": bool(result.get("new"))})

        if action == "crm.create_deal":
            body = {
                "properties": {
                    "dealname": payload["title"],
                    "pipeline": self.pipeline,
                    "dealstage": self._stage(payload.get("stage", "new")),
                    KEY_PROPERTY: idem_key,
                    **({"amount": str(payload["amount"])} if payload.get("amount") is not None else {}),
                },
                "associations": [
                    {
                        "to": {"id": payload["contact_id"]},
                        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": DEAL_TO_CONTACT}],
                    }
                ],
            }
            return self._create("deals", body, idem_key)

        if action == "crm.update_deal_stage":
            props = {"dealstage": self._stage(payload["stage"])}
            if payload.get("amount") is not None:
                props["amount"] = str(payload["amount"])
            r = request(self.client, "PATCH", f"/crm/v3/objects/deals/{payload['deal_id']}", json={"properties": props})
            if r.status_code != 200:
                raise fail_permanently(r, "deal update")
            return Receipt(self.app, str(r.json()["id"]))

        if action == "crm.add_note":
            body = {
                "properties": {
                    "hs_note_body": payload["body"],
                    "hs_timestamp": str(int(time.time() * 1000)),
                    KEY_PROPERTY: idem_key,
                },
                "associations": [
                    {
                        "to": {"id": payload["object_id"]},
                        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_DEAL}],
                    }
                ],
            }
            return self._create("notes", body, idem_key)

        raise PermanentError(f"HubSpot adapter does not support {action}")

    def _create(self, object_type: str, body: dict[str, Any], idem_key: str) -> Receipt:
        r = request(self.client, "POST", f"/crm/v3/objects/{object_type}", json=body)
        if r.status_code == 201:
            return Receipt(self.app, str(r.json()["id"]))
        if r.status_code in (400, 409) and KEY_PROPERTY in r.text:
            existing = self._find(object_type, idem_key)
            if existing:
                return Receipt(self.app, existing, {"adopted_on_conflict": True})
        raise fail_permanently(r, f"{object_type} create")

    def _find(self, object_type: str, idem_key: str) -> str | None:
        body = {
            "filterGroups": [{"filters": [{"propertyName": KEY_PROPERTY, "operator": "EQ", "value": idem_key}]}],
            "limit": 1,
            "properties": [KEY_PROPERTY],
        }
        r = request(self.client, "POST", f"/crm/v3/objects/{object_type}/search", json=body)
        if r.status_code != 200:
            raise fail_permanently(r, f"{object_type} search")
        results = r.json().get("results", [])
        return str(results[0]["id"]) if results else None

    def _stage(self, stage: str) -> str:
        try:
            return self.stage_ids[stage]
        except KeyError:
            raise PermanentError(f"no HubSpot dealstage id configured for {stage!r}") from None

    # ---- lookup / verify -----------------------------------------------------------
    def lookup(self, action: str, idem_key: str, payload: dict[str, Any]) -> Receipt | None:
        object_type = {"crm.create_deal": "deals", "crm.add_note": "notes"}.get(action)
        if object_type is None:
            return None  # upsert and stage updates are safe to repeat
        found = self._find(object_type, idem_key)
        return Receipt(self.app, found, {"found_by": KEY_PROPERTY}) if found else None

    def verify(self, action: str, receipt: Receipt, payload: dict[str, Any]) -> Verification:
        object_type, props, expected = {
            "crm.upsert_contact": ("contacts", ["email"], {"email": payload.get("email")}),
            "crm.create_deal": ("deals", ["dealname"], {"dealname": payload.get("title")}),
            "crm.update_deal_stage": (
                "deals",
                ["dealstage"],
                {"dealstage": self.stage_ids.get(payload.get("stage", ""))},
            ),
            "crm.add_note": ("notes", ["hs_note_body"], {"hs_note_body": payload.get("body")}),
        }[action]
        r = request(
            self.client,
            "GET",
            f"/crm/v3/objects/{object_type}/{receipt.external_id}",
            params={"properties": ",".join(props)},
        )
        if r.status_code == 404:
            return Verification(ok=False, mismatches=[f"{object_type} {receipt.external_id} not found"])
        if r.status_code != 200:
            raise fail_permanently(r, f"{object_type} read-back")
        return compare_fields(expected, r.json().get("properties", {}), props)
