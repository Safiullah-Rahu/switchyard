"""Signed session cookies and CSRF tokens for the console.

The session says who is signed in; every decision is still checked against
that person's roles by the approvals service. In production this sits behind
the company's SSO (Google Workspace / Microsoft Entra ID); the token sign-in
and the demo accounts are stand-ins for a reference build.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

COOKIE = "sy_session"
FLASH = "sy_flash"
TTL_SECONDS = 12 * 3600


def _mac(secret: bytes, purpose: str, value: str) -> str:
    return hmac.new(secret, f"{purpose}|{value}".encode(), hashlib.sha256).hexdigest()


def issue(secret: bytes, principal: str, now: float | None = None) -> str:
    expires = int((now or time.time()) + TTL_SECONDS)
    body = f"{principal}|{expires}"
    return f"{body}|{_mac(secret, 'session', body)}"


def read(secret: bytes, cookie: str | None, now: float | None = None) -> str | None:
    """Return the principal for a valid, unexpired cookie, else None."""
    if not cookie or cookie.count("|") != 2:
        return None
    principal, expires, signature = cookie.split("|")
    if not hmac.compare_digest(signature, _mac(secret, "session", f"{principal}|{expires}")):
        return None
    if not expires.isdigit() or int(expires) < (now or time.time()):
        return None
    return principal


def csrf_token(secret: bytes, cookie: str | None) -> str:
    return _mac(secret, "csrf", cookie or "anonymous")[:32]


def csrf_ok(secret: bytes, cookie: str | None, token: str | None) -> bool:
    return bool(cookie and token) and hmac.compare_digest(csrf_token(secret, cookie), token or "")


def pack_flash(kind: str, text: str) -> str:
    return urlsafe_b64encode(json.dumps({"kind": kind, "text": text}).encode()).decode()


def unpack_flash(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    try:
        data = json.loads(urlsafe_b64decode(value.encode()))
        return {"kind": str(data["kind"]), "text": str(data["text"])[:300]}
    except (ValueError, KeyError, TypeError):
        return None
