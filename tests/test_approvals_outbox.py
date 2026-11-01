import pytest

from switchyard.adapters.base import SimulatedCrash
from switchyard.approvals import ApprovalError
from switchyard.engine import Engine
from switchyard.outbox import IntentFailed, IntentRetry, IntentWaiting
from switchyard.policy import PolicyViolation

EMAIL = {"to": ["customer@example.com"], "cc": [], "subject": "Hello", "body": "Thanks for your enquiry."}


def propose(engine, action, payload, slot="main", step="s"):
    return engine.outbox.propose(
        run_id="run_t", step=step, slot=slot, action=action, payload=payload, proposed_by="test"
    )


def test_same_slot_is_the_same_intent(engine):
    a = propose(engine, "chat.post", {"channel": "#t", "text": "x"})
    b = propose(engine, "chat.post", {"channel": "#t", "text": "x"})
    assert a["id"] == b["id"] and a["idem_key"] == b["idem_key"]


def test_unregistered_action_cannot_be_proposed(engine):
    with pytest.raises(PolicyViolation):
        propose(engine, "bank.transfer", {"amount": 1})


def test_internal_action_runs_and_verifies(engine):
    intent = engine.outbox.process(propose(engine, "chat.post", {"channel": "#t", "text": "hi"})["id"])
    assert intent["status"] == "verified"
    assert len(engine.reads.chat_history("#t")) == 1


def test_external_email_waits_then_sends_once(engine):
    intent = propose(engine, "email.send", EMAIL)
    with pytest.raises(IntentWaiting) as waiting:
        engine.outbox.process(intent["id"])
    assert not engine.reads.email_search()
    engine.approvals.decide(waiting.value.approval["id"], "maya.patel", "approve")
    assert engine.outbox.process(intent["id"])["status"] == "verified"
    assert engine.outbox.process(intent["id"])["status"] == "verified"  # processing again is a no-op
    assert len(engine.reads.email_search()) == 1


def test_wrong_role_cannot_approve(engine):
    intent = propose(engine, "email.send", EMAIL)
    with pytest.raises(IntentWaiting) as waiting:
        engine.outbox.process(intent["id"])
    with pytest.raises(ApprovalError):
        engine.approvals.decide(waiting.value.approval["id"], "omar.haddad", "approve")  # finance ≠ external comms


def test_approval_is_bound_to_the_payload(engine):
    intent = propose(engine, "email.send", EMAIL, slot="m")
    with pytest.raises(IntentWaiting) as first:
        engine.outbox.process(intent["id"])
    engine.approvals.decide(first.value.approval["id"], "maya.patel", "approve")
    revised = propose(engine, "email.send", {**EMAIL, "body": "Different text"}, slot="m")
    assert revised["id"] == intent["id"] and revised["payload_hash"] != intent["payload_hash"]
    with pytest.raises(IntentWaiting) as second:
        engine.outbox.process(intent["id"])
    assert second.value.approval["id"] != first.value.approval["id"]
    assert not engine.reads.email_search()


def test_forged_signature_is_not_accepted(engine):
    intent = propose(engine, "email.send", EMAIL)
    with pytest.raises(IntentWaiting) as waiting:
        engine.outbox.process(intent["id"])
    engine.conn.execute(
        "UPDATE approvals SET status='approved', decided_by='maya.patel', decided_at=1, signature='abc' WHERE id=?",
        (waiting.value.approval["id"],),
    )
    assert engine.approvals.check(engine.outbox.get(intent["id"])).status == "invalid"
    with pytest.raises(IntentWaiting):
        engine.outbox.process(intent["id"])
    assert not engine.reads.email_search()


def test_pending_approvals_expire(engine, clock):
    intent = propose(engine, "email.send", EMAIL)
    with pytest.raises(IntentWaiting) as waiting:
        engine.outbox.process(intent["id"])
    clock.advance(73 * 3600)
    with pytest.raises(ApprovalError):
        engine.approvals.decide(waiting.value.approval["id"], "maya.patel", "approve")


def test_revoked_role_invalidates_an_old_approval(engine):
    intent = propose(engine, "email.send", EMAIL)
    with pytest.raises(IntentWaiting) as waiting:
        engine.outbox.process(intent["id"])
    engine.approvals.decide(waiting.value.approval["id"], "maya.patel", "approve")
    engine.policy.principals["maya.patel"]["roles"] = []
    assert engine.approvals.check(engine.outbox.get(intent["id"])).status == "invalid"


def test_transient_error_backs_off_then_succeeds(engine, clock):
    engine.reads.set_fault("crm", "create_deal", "transient")
    intent = propose(engine, "crm.create_deal", {"contact_id": "ct_1", "title": "Deal", "stage": "new"})
    with pytest.raises(IntentRetry) as retry:
        engine.outbox.process(intent["id"])
    with pytest.raises(IntentRetry):  # too early: still backing off, no call made
        engine.outbox.process(intent["id"])
    clock.advance(retry.value.delay + 0.1)
    assert engine.outbox.process(intent["id"])["status"] == "verified"
    assert [c["outcome"] for c in engine.reads.call_log("crm")] == ["503", "200"]


def test_ambiguous_timeout_reconciles_instead_of_resending(engine):
    engine.reads.set_fault("email", "send", "timeout_after_write")
    intent = propose(engine, "email.send", {**EMAIL, "to": ["ops@demo-energy.test"]})  # internal: no approval needed
    with pytest.raises(IntentRetry):
        engine.outbox.process(intent["id"])
    assert engine.outbox.get(intent["id"])["status"] == "executing"
    assert engine.outbox.process(intent["id"])["status"] == "verified"
    assert len(engine.reads.email_search()) == 1


def test_crash_after_write_is_adopted_by_a_new_process(engine, settings, clock):
    engine.reads.set_fault("docs", "create", "crash_after_write")
    intent = propose(engine, "docs.create", {"title": "T", "folder": "F", "body": "B", "shared_with": []})
    with pytest.raises(SimulatedCrash):
        engine.outbox.process(intent["id"])
    fresh = Engine(settings, clock=clock, worker_id="new-process")
    try:
        assert fresh.outbox.process(intent["id"])["status"] == "verified"
        assert len(fresh.reads.docs_find_by_property("switchyard_key", intent["idem_key"])) == 1
    finally:
        fresh.close()


def test_dropped_write_fails_verification_and_retry_rewrites_once(engine):
    engine.reads.set_fault("docs", "create", "drop")
    intent = propose(engine, "docs.create", {"title": "T", "folder": "F", "body": "B", "shared_with": []})
    with pytest.raises(IntentFailed) as failed:
        engine.outbox.process(intent["id"])
    assert failed.value.kind == "verification_failed"
    engine.outbox.reset_for_retry(intent["id"], "ops")
    assert engine.outbox.process(intent["id"])["status"] == "verified"
    assert len(engine.reads.docs_find_by_property("switchyard_key", intent["idem_key"])) == 1


def test_permanent_error_stops(engine):
    engine.reads.set_fault("calendar", "create_event", "permanent")
    intent = propose(
        engine,
        "calendar.create_event",
        {"title": "S", "start": "2026-11-03T08:30", "end": "2026-11-03T10:00", "attendees": []},
    )
    with pytest.raises(IntentFailed) as failed:
        engine.outbox.process(intent["id"])
    assert failed.value.kind == "tool_permanent"
