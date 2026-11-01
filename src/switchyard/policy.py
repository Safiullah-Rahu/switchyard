"""Action registry and risk classification.

The policy is code + config, evaluated by the executor at execution time.
Prompts can ask for anything; only this module decides whether a person must
approve it first.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class RiskClass(StrEnum):
    INTERNAL_ROUTINE = "internal_routine"
    EXTERNAL_COMMUNICATION = "external_communication"
    BINDING_COMMITMENT = "binding_commitment"
    SENSITIVE_DISCLOSURE = "sensitive_disclosure"


class PolicyViolation(Exception):
    def __init__(self, message: str, *, kind: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Classification:
    action: str
    app: str
    risk_class: str
    requires_approval: bool
    approver_roles: tuple[str, ...]
    expires_hours: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b")
_INTERNAL_MARKERS = re.compile(
    r"\b(internal only|do not share|cost price|our cost|gross margin|margin of|supplier price)\b",
    re.IGNORECASE,
)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def scan_sensitive(payload: Any, address_allowed: Callable[[str], bool]) -> list[str]:
    """Deterministic checks for content that must not leave the company unreviewed."""
    findings: list[str] = []
    for text in _strings(payload):
        if _IBAN.search(text):
            findings.append("contains what looks like a bank account number (IBAN)")
        for match in _CARD.finditer(text):
            digits = re.sub(r"\D", "", match.group())
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                findings.append("contains what looks like a payment card number")
                break
        if _SECRET.search(text):
            findings.append("contains what looks like an API credential")
        marker = _INTERNAL_MARKERS.search(text)
        if marker:
            findings.append(f"contains internal-only wording ({marker.group(0).lower()!r})")
        for address in _EMAIL.findall(text):
            if not address_allowed(address.lower()):
                findings.append(f"mentions a third-party address ({address})")
    return sorted(set(findings))


class Policy:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.company = data.get("company", {})
        self.classes: dict[str, dict[str, Any]] = data["classes"]
        self.actions: dict[str, dict[str, Any]] = data["actions"]
        self.agents: dict[str, dict[str, Any]] = data.get("agents", {})
        self.principals: dict[str, dict[str, Any]] = data.get("principals", {})
        for name, spec in self.actions.items():
            if spec["class"] not in self.classes:
                raise ValueError(f"action {name} uses unknown class {spec['class']}")
        self.internal_domains = {d.lower() for d in self.company.get("internal_domains", [])}

    # ---- lookups -----------------------------------------------------------
    def action_spec(self, action: str) -> dict[str, Any]:
        spec = self.actions.get(action)
        if spec is None:
            raise PolicyViolation(f"action {action!r} is not registered", kind="unregistered_action")
        return spec

    def is_internal_address(self, address: str) -> bool:
        return address.rsplit("@", 1)[-1].lower() in self.internal_domains

    def principal_roles(self, principal: str) -> set[str]:
        return set(self.principals.get(principal, {}).get("roles", []))

    def principal_display(self, principal: str) -> str:
        return self.principals.get(principal, {}).get("display", principal)

    def check_agent_may_propose(self, agent_key: str, action: str) -> None:
        self.action_spec(action)
        allowed = self.agents.get(agent_key, {}).get("may_propose", [])
        if action not in allowed:
            raise PolicyViolation(
                f"agent {agent_key!r} may not propose {action!r} (allowed: {allowed or 'none'})",
                kind="not_permitted",
            )

    # ---- classification ----------------------------------------------------
    def classify(self, action: str, payload: dict[str, Any]) -> Classification:
        spec = self.action_spec(action)
        found: list[tuple[str, str]] = [(spec["class"], f"{action} is registered as {spec['class']}")]

        if action == "email.send":
            recipients = [*payload.get("to", []), *payload.get("cc", [])]
            outside = [r for r in recipients if not self.is_internal_address(r)]
            if outside:
                found.append((RiskClass.EXTERNAL_COMMUNICATION, f"recipient outside the company: {', '.join(outside)}"))
            else:
                found = [(RiskClass.INTERNAL_ROUTINE, "every recipient is internal")]
            quote = payload.get("quote_total")
            if outside and quote is not None and quote >= self.company.get("binding_quote_threshold", 0):
                found.append(
                    (RiskClass.BINDING_COMMITMENT, f"carries a priced quote ({quote:,.0f}) — an offer to the customer")
                )
        elif action == "chat.post":
            if payload.get("channel") in self.company.get("external_channels", []):
                found.append(
                    (RiskClass.EXTERNAL_COMMUNICATION, f"channel {payload['channel']} is shared outside the company")
                )
        elif action == "docs.create":
            outside = [a for a in payload.get("shared_with", []) if not self.is_internal_address(a)]
            if outside:
                found.append((RiskClass.EXTERNAL_COMMUNICATION, f"shared outside the company: {', '.join(outside)}"))
        elif action == "calendar.create_event":
            outside = [a for a in payload.get("attendees", []) if not self.is_internal_address(a)]
            if outside:
                found.append(
                    (RiskClass.EXTERNAL_COMMUNICATION, f"invites people outside the company: {', '.join(outside)}")
                )

        if payload.get("commitment") is True:
            found.append((RiskClass.BINDING_COMMITMENT, "payload is marked as a commitment"))

        rank = lambda cls: self.classes[cls]["rank"]  # noqa: E731
        top = max((c for c, _ in found), key=rank)
        if rank(top) >= self.classes[RiskClass.EXTERNAL_COMMUNICATION]["rank"]:
            addressed = [*payload.get("to", []), *payload.get("cc", []), *payload.get("attendees", [])]
            if payload.get("customer_email"):
                addressed.append(payload["customer_email"])
            allowed = {a.lower() for a in addressed}
            allowed |= {a.lower() for a in payload.get("allowed_mentions", [])}
            content = {
                k: v
                for k, v in payload.items()
                if k not in {"to", "cc", "attendees", "allowed_mentions", "customer_email"}
            }
            for finding in scan_sensitive(content, lambda a: a in allowed or self.is_internal_address(a)):
                found.append((RiskClass.SENSITIVE_DISCLOSURE, finding))
            top = max((c for c, _ in found), key=rank)

        cls = self.classes[top]
        return Classification(
            action=action,
            app=spec["app"],
            risk_class=str(top),
            requires_approval=bool(cls.get("requires_approval", False)),
            approver_roles=tuple(cls.get("approver_roles", [])),
            expires_hours=float(cls.get("expires_hours", 72)),
            reasons=tuple(reason for _, reason in found),
        )
