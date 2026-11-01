import pytest

from switchyard.audit import AuditLog
from switchyard.checks import check_proposal
from switchyard.config import load_toml
from switchyard.db import connect
from switchyard.estimator import EstimateInputs, estimate
from switchyard.sandbox import TEMPLATES
from switchyard.specialists.offline import _compose
from switchyard.workflows.common import estimate_for_agent

CFG = load_toml("estimator.toml")
DISCLAIMER = TEMPLATES["commercial-solar-proposal"]["required_disclaimer"]
LEAD = {
    "contact_name": "Priya Shah",
    "company": "Northgate Bakery Ltd",
    "annual_consumption_kwh": 48000,
    "roof_area_m2": 600,
}


def bakery(**overrides):
    args = {
        "segment": "commercial",
        "region": "uk-south",
        "annual_consumption_kwh": 48000,
        "roof_area_m2": 600,
        "include_battery": True,
    }
    return estimate(EstimateInputs(**{**args, **overrides}), CFG)


def test_estimate_is_internally_consistent():
    e = bakery()
    assert e.panel_count * 0.43 == pytest.approx(e.system_kwp, abs=0.01)
    assert e.annual_generation_kwh == pytest.approx(e.system_kwp * 980, abs=10)
    assert e.payback_years == round(e.capex / e.annual_savings, 1)
    assert e.limited_by == "consumption"


def test_small_roof_limits_the_system():
    e = bakery(roof_area_m2=80)
    assert e.limited_by == "roof" and e.panel_count == int(80 * 0.65 / 1.95)


def test_estimate_refuses_to_guess():
    with pytest.raises(ValueError):
        bakery(annual_consumption_kwh=None, roof_area_m2=None)
    with pytest.raises(ValueError):
        bakery(region="mars")


def draft(wrong=False):
    est = bakery()
    return est, _compose(LEAD, estimate_for_agent(est), TEMPLATES["commercial-solar-proposal"], wrong).model_dump()


def test_correct_draft_passes():
    est, d = draft()
    report = check_proposal(d, est, DISCLAIMER)
    assert report.passed, report.summary()
    assert report.numbers_checked >= 10


def test_wrong_payback_is_caught():
    est, d = draft(wrong=True)
    report = check_proposal(d, est, DISCLAIMER)
    assert not report.passed
    assert {f.check for f in report.findings} >= {"figures_match", "unsupported_number"}


@pytest.mark.parametrize(
    ("mutate", "check"),
    [
        (lambda d: d.update(body_markdown=d["body_markdown"] + "\nSavings are guaranteed."), "forbidden_claim"),
        (lambda d: d.update(body_markdown=d["body_markdown"].replace(DISCLAIMER, "")), "disclaimer"),
        (lambda d: d.update(email_body=d["email_body"] + " Roughly $13,000 in dollars."), "currency"),
        (
            lambda d: d.update(body_markdown=d["body_markdown"] + "\nYou could also save 12,345 kWh more."),
            "unsupported_number",
        ),
    ],
)
def test_specific_problems_are_named(mutate, check):
    est, d = draft()
    mutate(d)
    report = check_proposal(d, est, DISCLAIMER)
    assert not report.passed and check in {f.check for f in report.findings}


def test_audit_chain_detects_edits_and_deletions(tmp_path):
    conn = connect(tmp_path / "a.db")
    log = AuditLog(conn)
    for i in range(5):
        log.record("thing.happened", actor="t", data={"i": i})
    assert log.verify().ok
    conn.execute("UPDATE audit SET data_json = '{\"i\": 99}' WHERE seq = 3")
    bad = log.verify()
    assert not bad.ok and bad.first_bad_seq == 3
    conn.execute("UPDATE audit SET data_json = '{\"i\": 2}' WHERE seq = 3")
    assert log.verify().ok
    conn.execute("DELETE FROM audit WHERE seq = 2")
    assert log.verify().first_bad_seq == 3
