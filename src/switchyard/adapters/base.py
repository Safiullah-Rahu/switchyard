"""The contract every integration implements.

Each adapter answers three questions for the outbox:

* execute — do the thing, passing the idempotency key to the destination in
  whatever form that API supports (unique property, client-supplied id,
  Message-ID header, message metadata …);
* lookup  — did a previous attempt with this key already land? (used after a
  crash or timeout, when we cannot know whether the write happened);
* verify  — read the destination back and confirm the write is really there
  and matches what we meant to write.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class AdapterError(Exception):
    """Base class for integration failures."""


class TransientError(AdapterError):
    """Safe to retry: nothing was written (rate limit, 5xx before commit, network down)."""


class PermanentError(AdapterError):
    """Retrying will not help (403, validation error, deleted object)."""


class AmbiguousOutcome(AdapterError):
    """The request may or may not have been applied (timeout after send, connection reset)."""


class SimulatedCrash(BaseException):
    """Test-only: stands in for the process dying mid-call. Not an Exception on purpose."""


@dataclass
class Receipt:
    app: str
    external_id: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Verification:
    ok: bool
    checked: dict[str, Any] = field(default_factory=dict)
    mismatches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Adapter(Protocol):
    app: str

    def execute(self, action: str, payload: dict[str, Any], idem_key: str) -> Receipt: ...

    def lookup(self, action: str, idem_key: str, payload: dict[str, Any]) -> Receipt | None: ...

    def verify(self, action: str, receipt: Receipt, payload: dict[str, Any]) -> Verification: ...


class AdapterRegistry:
    def __init__(self, adapters: list[Adapter] | None = None):
        self._by_app: dict[str, Adapter] = {}
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: Adapter) -> None:
        self._by_app[adapter.app] = adapter

    def for_app(self, app: str) -> Adapter:
        try:
            return self._by_app[app]
        except KeyError:
            raise PermanentError(f"no adapter registered for app {app!r}") from None

    def apps(self) -> list[str]:
        return sorted(self._by_app)


def compare_fields(expected: dict[str, Any], actual: dict[str, Any], fields: list[str]) -> Verification:
    mismatches = [
        f"{name}: expected {expected.get(name)!r}, found {actual.get(name)!r}"
        for name in fields
        if expected.get(name) != actual.get(name)
    ]
    return Verification(ok=not mismatches, checked={name: actual.get(name) for name in fields}, mismatches=mismatches)
