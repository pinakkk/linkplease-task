"""Keyword matching.

ASSIGNMENT: "Keyword matching is case-insensitive and matches anywhere in the
comment text." That is the whole contract — no word boundaries, no stemming, no
regex. "PRICEY" contains "price", so it matches; being cleverer than the spec
would silently disagree with the grader's truth data.
"""
import pytest

from tests.conftest import comment_event, encode, require, wait_until


@pytest.fixture
def matcher():
    return require("matcher", "matches", "handle_event")


# --- matches(): the pure function --------------------------------------------

@pytest.mark.parametrize(
    "keyword,text,expected,why",
    [
        ("PRICE", "PRICE", True, "exact"),
        ("PRICE", "price", True, "keyword upper, text lower"),
        ("price", "PRICE", True, "keyword lower, text upper"),
        ("PrIcE", "pRiCe", True, "mixed case both sides"),
        ("PRICE", "what is the PRICE?", True, "substring in a sentence"),
        ("PRICE", "PRICEY", True, "substring inside a longer word — spec says anywhere"),
        ("PRICE", "overpriced", True, "substring in the middle of a word"),
        ("PRICE", "PRICE please 🙏", True, "emoji in the text"),
        ("PRICE", "цена PRICE 价格", True, "surrounding unicode"),
        ("café", "Where is the CAFÉ?", True, "non-ascii keyword, case-insensitive"),
        ("PRICE", "how much?", False, "no match"),
        ("PRICE", "", False, "empty text"),
        ("PRICE", "pric", False, "partial keyword is not a match"),
        ("PRICE", "  spaced out  ", False, "whitespace-only near-miss"),
    ],
)
def test_matches(matcher, keyword, text, expected, why):
    assert matcher.matches(keyword, text) is expected, why


def test_matches_is_pure(matcher):
    """Calling it twice must give the same answer — no hidden state, no I/O."""
    assert matcher.matches("PRICE", "the PRICE") is True
    assert matcher.matches("PRICE", "the PRICE") is True


def test_matches_handles_none_text_without_crashing(matcher):
    """`data.text` can be absent on a malformed event; ingest must not die."""
    try:
        result = matcher.matches("PRICE", None)
    except (TypeError, AttributeError):
        pytest.fail("matches() must tolerate a missing/None text, not raise")
    assert result is False


# --- handle_event(): one job per matching rule -------------------------------

async def test_matching_comment_creates_one_job(matcher, create_rule, jobs):
    rule_id = await create_rule("PRICE")
    event = comment_event(text="what is the PRICE?", user_id="usr_a")
    await matcher.handle_event(event["event_id"], "comment.created", event["data"])

    rows = await jobs()
    assert len(rows) == 1
    assert rows[0]["rule_id"] == rule_id
    assert rows[0]["user_id"] == "usr_a"
    assert rows[0]["status"] == "QUEUED"


async def test_non_matching_comment_creates_no_job(matcher, create_rule, jobs):
    await create_rule("PRICE")
    event = comment_event(text="lovely photo", user_id="usr_a")
    await matcher.handle_event(event["event_id"], "comment.created", event["data"])
    assert await jobs() == []


async def test_multiple_rules_matching_one_comment_yield_one_job_each(
    matcher, create_rule, jobs
):
    """Two different rules both matching means two distinct obligations — the
    'no duplicates' rule is per (rule, user), not per user."""
    r1 = await create_rule("PRICE", "price list", rule_id="rule_price")
    r2 = await create_rule("SHIPPING", "shipping info", rule_id="rule_ship")
    await create_rule("REFUND", "refund policy", rule_id="rule_refund")

    event = comment_event(text="PRICE and SHIPPING please", user_id="usr_a")
    await matcher.handle_event(event["event_id"], "comment.created", event["data"])

    rows = await jobs()
    assert {r["rule_id"] for r in rows} == {r1, r2}
    assert len(rows) == 2


async def test_identity_is_user_id_not_username(matcher, create_rule, jobs):
    """ASSIGNMENT: 'user_id is the identity, not username. Usernames change.'
    The same user under a new handle must NOT get a second DM."""
    await create_rule("PRICE")
    e1 = comment_event(text="PRICE", user_id="usr_a", username="alice")
    e2 = comment_event(text="PRICE", user_id="usr_a", username="alice.renamed")
    await matcher.handle_event(e1["event_id"], "comment.created", e1["data"])
    await matcher.handle_event(e2["event_id"], "comment.created", e2["data"])

    rows = await jobs()
    assert len(rows) == 1, "a username change must not create a second obligation"


async def test_different_users_each_get_a_job(matcher, create_rule, jobs):
    await create_rule("PRICE")
    for user in ("usr_a", "usr_b", "usr_c"):
        e = comment_event(text="PRICE", user_id=user)
        await matcher.handle_event(e["event_id"], "comment.created", e["data"])
    assert len({r["user_id"] for r in await jobs()}) == 3


async def test_emoji_comment_matches_end_to_end(matcher, create_rule, jobs):
    """The literal example from ASSIGNMENT's webhook payload."""
    await create_rule("PRICE")
    event = comment_event(text="PRICE please 🙏", user_id="usr_emoji")
    await matcher.handle_event(event["event_id"], "comment.created", event["data"])
    assert len(await jobs()) == 1


async def test_no_rules_means_no_jobs(matcher, jobs):
    event = comment_event(text="PRICE")
    await matcher.handle_event(event["event_id"], "comment.created", event["data"])
    assert await jobs() == []
