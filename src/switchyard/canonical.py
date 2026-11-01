"""Canonical JSON, hashing and identifiers.

Approvals are bound to a payload *hash*, and idempotency keys are derived from
stable identifiers, so both need one exact serialisation.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def payload_hash(payload: Any) -> str:
    return sha256_hex(canonical_json(payload))


def idempotency_key(*parts: str) -> str:
    """Deterministic key for one logical side effect (run + step + slot)."""
    return "sy_" + sha256_hex("|".join(parts))[:32]


def new_id(prefix: str) -> str:
    """Short, roughly time-ordered identifier."""
    return f"{prefix}_{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None:
        return default
    return json.loads(text)
