"""Deterministic review of a drafted proposal.

These checks run in code and decide on their own. The LLM reviewer adds a
second opinion on tone and claims, but a draft whose numbers do not trace back
to the estimator cannot pass, whatever any model says.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .estimator import Estimate

_NUMBER = re.compile(
    r"(?P<cur>[£$€])?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:[\s-]?(?P<unit>kWp|kWh|MWh|years?|yrs?|%|tonnes?|panels?|m²|m2)\b|(?P<pct>%))?"
)
_FORBIDDEN = re.compile(
    r"\b(guarantee[sd]?|risk[- ]free|no risk|free electricity|100% free|zero bills)\b", re.IGNORECASE
)
_ALL_CURRENCIES = {"£", "$", "€"}
_UNITS = {
    "year": "years",
    "years": "years",
    "yr": "years",
    "yrs": "years",
    "tonne": "tonnes",
    "tonnes": "tonnes",
    "panel": "panels",
    "panels": "panels",
    "m²": "m2",
    "m2": "m2",
    "kwp": "kWp",
    "kwh": "kWh",
    "mwh": "MWh",
    "%": "%",
}

KEY_FIGURES = ("system_kwp", "annual_generation_kwh", "annual_savings", "capex", "payback_years")
KEY_LABELS = {
    "system_kwp": "system size",
    "annual_generation_kwh": "annual generation",
    "annual_savings": "annual savings",
    "capex": "installed cost",
    "payback_years": "payback",
}


@dataclass
class Finding:
    severity: Literal["blocker", "minor"]
    check: str
    message: str


@dataclass
class CheckReport:
    passed: bool
    findings: list[Finding] = field(default_factory=list)
    numbers_checked: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        if self.passed:
            return f"all checks passed ({self.numbers_checked} numbers traced to the estimate)"
        return "; ".join(f.message for f in self.findings if f.severity == "blocker")


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.005 * abs(b), 0.051)


def _mentions(text: str) -> list[tuple[float, str | None, str]]:
    out = []
    for m in _NUMBER.finditer(text):
        value = float(m.group("num").replace(",", ""))
        unit = m.group("unit") or ("%" if m.group("pct") else None)
        if m.group("cur"):
            unit = "currency"
        elif unit:
            unit = _UNITS[unit.lower()]
        out.append((value, unit, m.group(0).strip()))
    return out


def check_proposal(draft: dict[str, Any], est: Estimate, required_disclaimer: str) -> CheckReport:
    findings: list[Finding] = []
    figures = est.figures()
    body = draft["body_markdown"]
    text = "\n".join([draft["executive_summary"], body, draft["email_subject"], draft["email_body"]])

    # 1. Every figure the writer says it cited must be the estimator's figure.
    for cited in draft.get("figures", []):
        key, value = cited["key"], float(cited["value"])
        if key not in figures:
            findings.append(Finding("blocker", "figures_match", f"cites {key!r}, which the estimate does not contain"))
        elif not _close(value, figures[key][0]):
            findings.append(
                Finding("blocker", "figures_match", f"{key} cited as {value:g}; the estimate says {figures[key][0]:g}")
            )

    # 2. Key figures must actually appear in the document body.
    body_mentions = _mentions(body)
    for key in KEY_FIGURES:
        expected, unit = figures[key]
        if not any(_close(v, expected) and u == unit for v, u, _ in body_mentions):
            shown = f"{est.symbol}{expected:,.0f}" if unit == "currency" else f"{expected:g} {unit}"
            findings.append(
                Finding(
                    "blocker", "key_figures_present", f"the proposal does not state the {KEY_LABELS[key]} ({shown})"
                )
            )

    # 3. Any number with a unit must trace to a figure with that unit.
    allowed: dict[str, list[float]] = {}
    for value, unit in figures.values():
        allowed.setdefault(unit, []).append(value)
        if unit == "kWh":
            allowed.setdefault("MWh", []).append(value / 1000)
    mentions = _mentions(text)
    checked = 0
    for value, unit, raw in mentions:
        if unit is None or unit == "m2":
            continue
        checked += 1
        if not any(_close(value, a) for a in allowed.get(unit, [])):
            findings.append(
                Finding("blocker", "unsupported_number", f"states {raw!r}, which no estimate figure supports")
            )

    # 4. One currency only.
    others = {c for c in _ALL_CURRENCIES if c in text} - {est.symbol}
    if others:
        findings.append(
            Finding("blocker", "currency", f"mixes currencies ({', '.join(sorted(others))} alongside {est.symbol})")
        )

    # 5. No promises.
    for m in _FORBIDDEN.finditer(text):
        findings.append(
            Finding("blocker", "forbidden_claim", f"makes a claim the company does not allow: {m.group(0)!r}")
        )

    # 6. The disclaimer must be there, word for word.
    if required_disclaimer.lower() not in body.lower():
        findings.append(Finding("blocker", "disclaimer", f"missing the required disclaimer: {required_disclaimer!r}"))
    if "estimate" not in draft["email_body"].lower():
        findings.append(Finding("minor", "disclaimer", "the email does not say the figures are estimates"))

    unique = list({(f.check, f.message): f for f in findings}.values())
    return CheckReport(
        passed=not any(f.severity == "blocker" for f in unique), findings=unique, numbers_checked=checked
    )
