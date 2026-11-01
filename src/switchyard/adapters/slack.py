"""Slack adapter (Web API: chat.postMessage, conversations.history).

Idempotency strategy: Slack has no idempotency key on chat.postMessage, so
the key travels in message `metadata`; before (re)posting, recent channel
history is scanned for it (`include_all_metadata=true`). Verification reads
the exact message back by its `ts`.

Slack answers HTTP 200 with `{"ok": false, "error": ...}` for most failures,
so the error string — not the status code — decides retry vs. stop.

Status: contract-tested against mocked HTTP (tests/test_http_adapters.py).
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import PermanentError, Receipt, TransientError, Verification
from .http import request

EVENT_TYPE = "switchyard_action"
RETRYABLE = {"ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout"}


class SlackChatAdapter:
    app = "chat"

    def __init__(
        self,
        token: str,
        *,
        channel_ids: dict[str, str],
        base_url: str = "https://slack.com/api",
        client: httpx.Client | None = None,
        history_window: int = 100,
    ):
        self.channel_ids = channel_ids
        self.history_window = history_window
        self.client = client or httpx.Client(base_url=base_url, timeout=httpx.Timeout(10.0, connect=5.0))
        self.client.headers["Authorization"] = f"Bearer {token}"

    def _channel(self, name: str) -> str:
        try:
            return self.channel_ids[name]
        except KeyError:
            raise PermanentError(f"no Slack channel id configured for {name!r}") from None

    def _call(self, method: str, http_method: str = "POST", **kwargs: Any) -> dict[str, Any]:
        r = request(self.client, http_method, f"/{method}", **kwargs)
        data = r.json()
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            if error in RETRYABLE:
                raise TransientError(f"slack {method}: {error}")
            raise PermanentError(f"slack {method}: {error}")
        return data

    def execute(self, action: str, payload: dict[str, Any], idem_key: str) -> Receipt:
        if action != "chat.post":
            raise PermanentError(f"Slack adapter does not support {action}")
        existing = self.lookup(action, idem_key, payload)
        if existing:
            return existing
        data = self._call(
            "chat.postMessage",
            json={
                "channel": self._channel(payload["channel"]),
                "text": payload["text"],
                "metadata": {"event_type": EVENT_TYPE, "event_payload": {"idempotency_key": idem_key}},
            },
        )
        return Receipt(self.app, data["ts"], {"channel": data["channel"]})

    def lookup(self, action: str, idem_key: str, payload: dict[str, Any]) -> Receipt | None:
        channel = self._channel(payload["channel"])
        data = self._call(
            "conversations.history",
            "GET",
            params={"channel": channel, "limit": self.history_window, "include_all_metadata": "true"},
        )
        for message in data.get("messages", []):
            meta = message.get("metadata") or {}
            if (
                meta.get("event_type") == EVENT_TYPE
                and meta.get("event_payload", {}).get("idempotency_key") == idem_key
            ):
                return Receipt(self.app, message["ts"], {"channel": channel, "found_by": "metadata"})
        return None

    def verify(self, action: str, receipt: Receipt, payload: dict[str, Any]) -> Verification:
        channel = receipt.details.get("channel") or self._channel(payload["channel"])
        data = self._call(
            "conversations.history",
            "GET",
            params={"channel": channel, "latest": receipt.external_id, "inclusive": "true", "limit": 1},
        )
        messages = [m for m in data.get("messages", []) if m.get("ts") == receipt.external_id]
        if not messages:
            return Verification(ok=False, mismatches=[f"message {receipt.external_id} not found in {channel}"])
        if messages[0].get("text") != payload["text"]:
            return Verification(
                ok=False, checked={"ts": receipt.external_id}, mismatches=["posted text differs from the intended text"]
            )
        return Verification(ok=True, checked={"ts": receipt.external_id, "channel": channel})
