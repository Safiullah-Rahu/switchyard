import json

import pytest
from fastapi.testclient import TestClient

from switchyard.api import create_app, sign
from switchyard.demo.fixtures import message
from switchyard.outbox import IntentWaiting


def client_for(settings):
    return TestClient(create_app(settings))


def post(client, settings, body, event_id="evt-1", signature=None):
    raw = json.dumps(body).encode()
    return client.post(
        "/webhooks/inbound",
        content=raw,
        headers={
            "X-Switchyard-Signature": signature or sign(settings.webhook_secret, raw),
            "X-Switchyard-Event-Id": event_id,
        },
    )


def test_unsigned_or_badly_signed_webhooks_are_rejected(settings):
    client = client_for(settings)
    assert post(client, settings, message("bakery"), signature="sha256=" + "0" * 64).status_code == 401
    raw = json.dumps(message("bakery")).encode()
    assert client.post("/webhooks/inbound", content=raw).status_code == 401
    assert client.get("/runs").json() == []


def test_webhook_redelivery_is_idempotent(settings):
    client = client_for(settings)
    first = post(client, settings, message("bakery"))
    second = post(client, settings, message("bakery"))
    assert first.status_code == 202 and not first.json()["duplicate"]
    assert second.json() == {"duplicate": True, "reason": "event id already received", "run_id": first.json()["run_id"]}
    assert len(client.get("/runs").json()) == 1


def test_missing_fields_are_422(settings):
    client = client_for(settings)
    assert post(client, settings, {"from_email": "a@example.com"}).status_code == 422


def test_decisions_need_a_token_with_the_right_role(settings):
    app = create_app(settings)
    client = TestClient(app)
    engine = app.state.engine
    intent = engine.outbox.propose(
        run_id="r",
        step="s",
        slot="m",
        action="email.send",
        payload={"to": ["a@example.com"], "subject": "s", "body": "b"},
        proposed_by="t",
    )
    with pytest.raises(IntentWaiting):
        engine.outbox.process(intent["id"])
    approval = engine.approvals.list("pending")[0]
    url = f"/approvals/{approval['id']}/decision"
    assert client.post(url, json={"decision": "approve"}).status_code == 401
    finance = {"Authorization": f"Bearer {settings.token_for('omar.haddad')}"}
    assert client.post(url, json={"decision": "approve"}, headers=finance).status_code == 403
    sales = {"Authorization": f"Bearer {settings.token_for('maya.patel')}"}
    ok = client.post(url, json={"decision": "approve", "note": "fine"}, headers=sales)
    assert ok.status_code == 200 and ok.json()["decided_by"] == "maya.patel"


def test_console_and_health_render(settings):
    client = client_for(settings)
    assert client.get("/health").json()["ok"] is True
    page = client.get("/")
    assert page.status_code == 200 and "operations console" in page.text
    assert client.get("/audit/verify").json()["ok"] is True
