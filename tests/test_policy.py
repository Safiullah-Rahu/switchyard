import pytest

from switchyard.config import load_toml
from switchyard.policy import Policy, PolicyViolation

policy = Policy(load_toml("policy.toml"))


def test_internal_recipients_make_email_routine():
    c = policy.classify("email.send", {"to": ["ops@demo-energy.test"], "subject": "x", "body": "y"})
    assert c.risk_class == "internal_routine" and not c.requires_approval


def test_external_email_needs_approval():
    c = policy.classify("email.send", {"to": ["a@example.com"], "subject": "x", "body": "hello"})
    assert c.risk_class == "external_communication" and c.requires_approval
    assert "sales_lead" in c.approver_roles


def test_priced_quote_is_a_binding_commitment():
    c = policy.classify(
        "email.send", {"to": ["a@example.com"], "subject": "Quote", "body": "see attached", "quote_total": 54900}
    )
    assert c.risk_class == "binding_commitment"
    assert set(c.approver_roles) == {"sales_lead", "finance"}


def test_internal_wording_to_outsiders_is_sensitive():
    c = policy.classify("email.send", {"to": ["a@example.com"], "subject": "x", "body": "Our gross margin is 32%"})
    assert c.risk_class == "sensitive_disclosure" and c.approver_roles == ("ops_manager",)


def test_internal_wording_inside_the_company_is_fine():
    c = policy.classify("chat.post", {"channel": "#sales", "text": "Our gross margin on this job is 32%"})
    assert c.risk_class == "internal_routine"


def test_shared_channel_is_external():
    c = policy.classify("chat.post", {"channel": "#customer-shared", "text": "hello"})
    assert c.risk_class == "external_communication"


@pytest.mark.parametrize(
    "body",
    [
        "Pay to GB82 WEST 1234 5698 7654 32 please",
        "Card 4111 1111 1111 1111 exp 12/29",
        "Use key sk-proj-ABCDEFGHIJKLMNOPQRSTUV",
        "Copying bob@othercustomer.example.org for reference",
    ],
)
def test_dlp_findings_raise_external_actions_to_sensitive(body):
    c = policy.classify("email.send", {"to": ["a@example.com"], "subject": "x", "body": body})
    assert c.risk_class == "sensitive_disclosure", c.reasons


def test_customer_on_invoice_is_not_a_third_party():
    c = policy.classify("billing.issue_invoice", {"customer_email": "a@example.com", "amount": 10, "currency": "GBP"})
    assert c.risk_class == "binding_commitment"


def test_unregistered_actions_are_refused():
    with pytest.raises(PolicyViolation) as exc:
        policy.classify("bank.transfer", {})
    assert exc.value.kind == "unregistered_action"


def test_agent_allowlist():
    policy.check_agent_may_propose("lead_qualifier", "chat.post")
    with pytest.raises(PolicyViolation) as exc:
        policy.check_agent_may_propose("lead_qualifier", "email.send")
    assert exc.value.kind == "not_permitted"
