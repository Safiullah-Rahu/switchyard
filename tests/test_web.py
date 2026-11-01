"""The operations console: pages render, sign-in works, and every action is guarded."""

import re

import pytest
from fastapi.testclient import TestClient

from switchyard.api import create_app
from switchyard.config import load_settings

SURVEY = {
    "deal_id": "dl_seed_rivera",
    "customer_email": "j.rivera@harbourcold.example.com",
    "survey": {
        "roof_ok": True,
        "shading": "low",
        "confirmed_kwp": 120.0,
        "notes": "Roof sound.",
        "surveyor": "Sam Okafor",
    },
    "deposit_pct": 20,
}


@pytest.fixture
async def console(settings):
    """A console with one run waiting on a deposit-invoice approval (no model calls needed)."""
    app = create_app(settings)
    engine = app.state.engine
    run_id, _ = engine.start_run("survey_to_install", SURVEY)
    await engine.run_until_idle(timeout=20)
    approval = engine.approvals.list("pending")[0]
    return TestClient(app), engine, run_id, approval


def sign_in(client, who):
    return client.post("/login/demo", data={"principal": who, "next": "/"})


def csrf(client, path):
    return re.search(r'<meta name="csrf-token" content="([0-9a-f]+)">', client.get(path).text).group(1)


async def test_every_page_renders_signed_out(console):
    client, _, run_id, approval = console
    for path in ["/", "/approvals", "/approvals?view=decided", "/runs", "/runs?status=waiting", f"/runs/{run_id}",
                 f"/approvals/{approval['id']}", "/exceptions", "/exceptions?view=resolved", "/audit", f"/audit?run={run_id}",
                 "/costs", "/login"]:  # fmt: skip
        page = client.get(path)
        assert page.status_code == 200, path
        assert "Switchyard" in page.text
    assert "Sign in to decide" in client.get(f"/approvals/{approval['id']}").text
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/runs/run_nope").status_code == 404


async def test_invoice_page_shows_the_exact_payload_and_who_may_approve(console):
    client, _, _, approval = console
    page = client.get(f"/approvals/{approval['id']}").text
    assert "£19,200" in page and "20% deposit" in page
    assert "Maya Patel" in page and "Omar Haddad" in page  # sales lead or finance
    assert "Lena Brandt" not in page.split("Who can approve")[1].split("</ul>")[0]


async def test_finance_approves_from_the_console(console):
    client, engine, run_id, approval = console
    assert "Signed in as Omar Haddad" in sign_in(client, "omar.haddad").text
    token = csrf(client, f"/approvals/{approval['id']}")
    done = client.post(f"/approvals/{approval['id']}/decide", data={"decision": "approve", "note": "ok", "csrf": token})
    assert done.status_code == 200 and "Approved" in done.text
    assert engine.approvals.get(approval["id"])["decided_by"] == "omar.haddad"
    await engine.run_until_idle(timeout=20)
    assert engine.get_run(run_id)["status"] == "succeeded"


async def test_actions_need_a_valid_csrf_token(console):
    client, engine, _, approval = console
    sign_in(client, "maya.patel")
    forged = client.post(f"/approvals/{approval['id']}/decide", data={"decision": "approve", "csrf": "0" * 32})
    assert forged.status_code == 403
    assert engine.approvals.get(approval["id"])["status"] == "pending"


async def test_wrong_role_is_told_and_nothing_changes(console):
    client, engine, _, approval = console
    sign_in(client, "lena.brandt")  # operations manager cannot approve a binding commitment
    page = client.get(f"/approvals/{approval['id']}").text
    assert "This needs Sales lead or Finance" in page and "Approve and send" not in page
    token = csrf(client, "/exceptions")
    result = client.post(f"/approvals/{approval['id']}/decide", data={"decision": "approve", "csrf": token})
    assert "lacks a required role" in result.text
    assert engine.approvals.get(approval["id"])["status"] == "pending"


async def test_signed_out_actions_redirect_to_sign_in(console):
    client, _, _, approval = console
    result = client.post(f"/approvals/{approval['id']}/decide", data={"decision": "approve"}, follow_redirects=False)
    assert result.status_code == 303 and result.headers["location"].startswith("/login")


async def test_token_sign_in_and_sign_out(console, settings):
    client, *_ = console
    assert "That token is not valid" in client.post("/login", data={"token": "sy_wrong", "next": "/"}).text
    signed = client.post("/login", data={"token": settings.token_for("maya.patel"), "next": "/runs"})
    assert signed.url.path == "/runs" and "Maya Patel" in signed.text
    assert "Signed out" in client.post("/logout").text
    assert "Sign in to decide" in client.get("/").text


async def test_session_cookie_cannot_be_forged(console):
    client, *_ = console
    client.cookies.set("sy_session", "maya.patel|9999999999|deadbeef")
    assert "Sign in to decide" in client.get("/").text


async def test_live_mode_has_no_demo_sign_in(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.delenv("SWITCHYARD_DEMO_SIGNIN", raising=False)
    client = TestClient(create_app(load_settings(tmp_path / "live", mode="live")))
    assert "Demo accounts" not in client.get("/login").text
    assert client.post("/login/demo", data={"principal": "maya.patel"}).status_code == 404
