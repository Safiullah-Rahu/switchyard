"""HTTP error mapping shared by the real-API adapters.

The mapping matters more than it looks: a timeout on a *write* is ambiguous
(the server may have applied it), so it must not be retried blindly — the
outbox reconciles with `lookup` first. A refused connection never reached the
server, so it is safe to retry.
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import AmbiguousOutcome, PermanentError, TransientError

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        response = client.request(method, url, **kwargs)
    except httpx.ConnectError as exc:
        raise TransientError(f"{method} {url}: connection failed ({exc})") from exc
    except (httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError) as exc:
        if method.upper() in WRITE_METHODS:
            raise AmbiguousOutcome(f"{method} {url}: {type(exc).__name__} after the request was sent") from exc
        raise TransientError(f"{method} {url}: {type(exc).__name__}") from exc
    if response.status_code == 429 or response.status_code >= 500:
        retry_after = response.headers.get("Retry-After")
        raise TransientError(
            f"{method} {url}: HTTP {response.status_code}" + (f" (retry after {retry_after}s)" if retry_after else "")
        )
    return response


def fail_permanently(response: httpx.Response, what: str) -> PermanentError:
    try:
        detail = response.json().get("message") or response.text[:200]
    except ValueError:
        detail = response.text[:200]
    return PermanentError(f"{what}: HTTP {response.status_code} {detail}")
