"""Part B: reject forged webhooks.

The header is `X-PseudoGram-Signature: sha256=<hex>`, an HMAC-SHA256 of the RAW
request body keyed with our API key (ASSIGNMENT, "Webhook payload"). Two things
have to be true and both are easy to get subtly wrong:

* the HMAC must be over the raw bytes, never over a re-serialised parse — one
  whitespace difference and every legitimate request fails;
* a forged request must not merely fail to produce a DM, it must not write an
  `events` row at all. Otherwise an attacker can poison our dedup ledger with
  event_ids and make us drop the real deliveries when they arrive.
"""
import hashlib
import hmac

import pytest

from tests.conftest import TEST_API_KEY, comment_event, encode, sign

from app import webhook


# --- verify_signature() as a pure function ------------------------------------

def test_valid_signature_passes():
    raw = b'{"event_id":"evt_1"}'
    assert webhook.verify_signature(raw, sign(raw), TEST_API_KEY) is True


def test_missing_header_rejected():
    raw = b'{"event_id":"evt_1"}'
    assert webhook.verify_signature(raw, None, TEST_API_KEY) is False
    assert webhook.verify_signature(raw, "", TEST_API_KEY) is False


def test_wrong_secret_rejected():
    raw = b'{"event_id":"evt_1"}'
    forged = "sha256=" + hmac.new(b"not_the_key", raw, hashlib.sha256).hexdigest()
    assert webhook.verify_signature(raw, forged, TEST_API_KEY) is False


def test_single_mutated_byte_rejected():
    """The whole point of an HMAC: one byte of tampering invalidates it."""
    raw = b'{"event_id":"evt_1","amount":100}'
    header = sign(raw)
    mutated = raw.replace(b"100", b"900")
    assert len(mutated) == len(raw)
    assert webhook.verify_signature(mutated, header, TEST_API_KEY) is False


def test_sha256_prefix_is_optional():
    """Documented form is `sha256=<hex>`; a bare digest must verify too, so a
    slightly different sender does not silently lose every event."""
    raw = b'{"event_id":"evt_1"}'
    bare = hmac.new(TEST_API_KEY.encode(), raw, hashlib.sha256).hexdigest()
    assert webhook.verify_signature(raw, bare, TEST_API_KEY) is True
    assert webhook.verify_signature(raw, "sha256=" + bare, TEST_API_KEY) is True


def test_garbage_header_rejected():
    raw = b'{"event_id":"evt_1"}'
    for bad in ("sha256=", "sha256=zzzz", "deadbeef", "sha1=abc", "   "):
        assert webhook.verify_signature(raw, bad, TEST_API_KEY) is False


def test_constant_time_compare_is_used():
    """A naive `==` leaks the expected digest one byte at a time under timing
    analysis. Assert the module actually calls `hmac.compare_digest`."""
    import inspect

    source = inspect.getsource(webhook.verify_signature)
    assert "compare_digest" in source, (
        "verify_signature must use hmac.compare_digest, not =="
    )


# --- The route ----------------------------------------------------------------

async def _events(pool):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM events")


async def test_route_accepts_correctly_signed_request(api, pool, signatures_on):
    raw = encode(comment_event())
    resp = await api.post(
        "/webhook", content=raw, headers={"X-PseudoGram-Signature": sign(raw)}
    )
    assert resp.status_code == 200
    assert len(await _events(pool)) == 1


@pytest.mark.parametrize(
    "header_factory,label",
    [
        (lambda raw: None, "missing header"),
        (lambda raw: "sha256=" + "0" * 64, "wrong digest"),
        (
            lambda raw: "sha256="
            + hmac.new(b"wrong_secret", raw, hashlib.sha256).hexdigest(),
            "wrong secret",
        ),
    ],
)
async def test_route_rejects_forged_and_writes_nothing(
    api, pool, signatures_on, header_factory, label
):
    raw = encode(comment_event())
    header = header_factory(raw)
    headers = {"X-PseudoGram-Signature": header} if header else {}
    resp = await api.post("/webhook", content=raw, headers=headers)

    assert resp.status_code == 401, f"{label} should be rejected"
    rows = await _events(pool)
    assert rows == [], (
        f"{label}: a forged request wrote an events row — an attacker could "
        "poison the dedup ledger and make us drop the genuine redelivery"
    )


async def test_route_rejects_body_tampered_in_flight(api, pool, signatures_on):
    """Signature computed over the original body, a different body delivered."""
    original = encode(comment_event(text="PRICE please"))
    header = sign(original)
    tampered = encode(comment_event(text="PRICE please!!"))

    resp = await api.post(
        "/webhook", content=tampered, headers={"X-PseudoGram-Signature": header}
    )
    assert resp.status_code == 401
    assert await _events(pool) == []
