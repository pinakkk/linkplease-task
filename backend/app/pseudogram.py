"""Typed client for the PseudoGram mock API.

Two calls matter: `send_dm` (costs a request against their 10/60s limit) and
`get_dm` (free, per ASSIGNMENT). Neither ever raises on an HTTP status — they
classify the response into an outcome the worker can branch on, because every
non-2xx here is an expected, designed-for event rather than a bug.

One AsyncClient is shared process-wide so connections are pooled; creating a
client per request would exhaust sockets during a 500-event burst.
"""
import logging
from typing import NamedTuple

import httpx

from . import config

log = logging.getLogger("linkplease.pseudogram")

# How much of an error body we put in the log / last_error. Enough to debug a
# 400, short enough that a runaway HTML error page cannot flood the log.
_BODY_LOG_LIMIT = 500

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Lazily create the one shared client. Lazy rather than at import time so
    importing this module never depends on config being fully loaded."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.PSEUDOGRAM_BASE_URL,
            headers={"X-API-Key": config.PSEUDOGRAM_API_KEY},
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
    return _client


async def aclose() -> None:
    """Close the shared client. Called from the FastAPI lifespan shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class SendResult(NamedTuple):
    """Outcome of one POST /v1/dm/send.

    outcome is one of:
      "accepted"        - 2xx carrying a dm_id; the DM is queued on their side
      "rate_limited"    - 429; retry after `retry_after` seconds
      "server_error"    - 5xx; safe to retry with the same idempotency key
      "bad_request"     - 400; our payload is wrong, retrying cannot help
      "transport_error" - timeout / connection failure. The request MAY have
                          landed, so the caller MUST retry with the SAME
                          idempotency key rather than treating it as "not sent".
    """
    outcome: str
    dm_id: str | None = None
    retry_after: float | None = None
    status_code: int | None = None
    detail: str | None = None


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After in seconds. Anything we cannot read as a number becomes None
    and the caller falls back to its own default — a garbage header must never
    crash the send loop or produce a negative sleep."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def _truncate(text: str) -> str:
    if len(text) <= _BODY_LOG_LIMIT:
        return text
    return text[:_BODY_LOG_LIMIT] + "...(truncated)"


async def send_dm(
    recipient_user_id: str,
    message: str,
    comment_id: str,
    idempotency_key: str,
) -> SendResult:
    """POST /v1/dm/send. Costs one request against the rate budget whatever the
    response is, so the caller records the send BEFORE calling this."""
    payload = {
        "recipient_user_id": recipient_user_id,
        "message": message,
        "comment_id": comment_id,
    }
    try:
        response = await get_client().post(
            "/v1/dm/send",
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        # We do not know whether they received it. Same-key retry is the only
        # safe move (BLUEPRINT §5 row 6).
        log.warning("send_dm transport error key=%s: %r", idempotency_key, exc)
        return SendResult("transport_error", detail=repr(exc))

    status = response.status_code
    body_text = _truncate(response.text or "")

    if 200 <= status < 300:
        dm_id = None
        try:
            body = response.json()
            if isinstance(body, dict):
                dm_id = body.get("dm_id")
        except ValueError:
            body = None
        if isinstance(dm_id, str) and dm_id:
            return SendResult("accepted", dm_id=dm_id, status_code=status)
        # 2xx with no dm_id: we cannot reconcile something we cannot name. Treat
        # it as a server-side anomaly and retry with the same key — their
        # idempotency returns the original dm_id if it really did land.
        log.error("send_dm %s with no dm_id key=%s body=%s",
                  status, idempotency_key, body_text)
        return SendResult("server_error", status_code=status, detail=body_text)

    log.warning("send_dm non-2xx status=%s key=%s body=%s",
                status, idempotency_key, body_text)

    if status == 429:
        return SendResult(
            "rate_limited",
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
            status_code=status,
            detail=body_text,
        )
    if status == 400:
        return SendResult("bad_request", status_code=status, detail=body_text)
    if status >= 500:
        return SendResult("server_error", status_code=status, detail=body_text)
    # 401/403/404 and friends: not retryable in any useful way, and a retry loop
    # against a bad API key would burn the whole budget. Treat like a 400.
    return SendResult("bad_request", status_code=status, detail=body_text)


async def get_dm(dm_id: str) -> str | None:
    """GET /v1/dm/{dm_id} -> "queued" | "delivered" | "failed", or None.

    None means "we learned nothing" (transport failure, non-2xx, unparseable
    body). The caller keeps the job in AWAITING_CONFIRM and polls again — a read
    failure must never be mistaken for a delivery failure.

    Reads do not count against the rate limit (ASSIGNMENT), so this never
    touches the send budget.
    """
    try:
        response = await get_client().get(f"/v1/dm/{dm_id}")
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        log.warning("get_dm transport error dm_id=%s: %r", dm_id, exc)
        return None

    if response.status_code >= 300:
        log.warning("get_dm non-2xx status=%s dm_id=%s body=%s",
                    response.status_code, dm_id, _truncate(response.text or ""))
        return None
    try:
        body = response.json()
    except ValueError:
        log.warning("get_dm unparseable body dm_id=%s", dm_id)
        return None
    if not isinstance(body, dict):
        return None
    status = body.get("status")
    return status if isinstance(status, str) else None
