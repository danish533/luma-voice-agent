"""Redis idempotency and the availability cache.

Run against a real Redis if REDIS_URL is set; otherwise fakeredis, which
implements SET NX, TTL and SCAN faithfully enough for these guarantees.

The distinction the whole module rests on: idempotency is a *correctness*
mechanism and must be exact, while the slot cache is a *latency* mechanism that
is allowed to be stale — but only on the read path.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

from luma.store import Cache

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def cache():
    url = os.getenv("REDIS_URL")
    if url:
        c = Cache(url)
    else:
        import fakeredis.aioredis

        c = Cache(None, client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    await c._client.flushall()
    yield c
    await c.aclose()


async def test_only_one_caller_wins_a_booking_key(cache: Cache) -> None:
    """The point of SET NX. Two workers racing the same booking -- a redial that
    lands elsewhere, or a retry after a restart -- must not both write."""
    first = await cache.claim_booking("k1", {"pending": True})
    assert first is None, "the first claim wins the key"

    await cache.store_booking("k1", {"confirmation_code": "LUMA-7422"})

    second = await cache.claim_booking("k1", {"pending": True})
    assert second == {"confirmation_code": "LUMA-7422"}, "the loser gets the winner's record"


async def test_concurrent_claims_produce_exactly_one_winner(cache: Cache) -> None:
    """Check-then-set would leave a window between the two operations in which
    both callers decide they were first."""
    results = await asyncio.gather(*(cache.claim_booking("k2", {"n": i}) for i in range(20)))
    winners = [r for r in results if r is None]
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"


async def test_a_booking_evicts_only_the_slot_it_consumed(cache: Cache) -> None:
    """Coherence has to be surgical. Flushing the whole date would throw away
    availability for every other party size, and the next caller pays for a
    full re-probe."""
    await cache.put_slots("2026-08-14", 2, ["17:30", "18:00", "19:30"])
    await cache.put_slots("2026-08-14", 4, ["17:30", "19:30"])
    await cache.put_slots("2026-08-15", 2, ["18:00"])

    await cache.drop_slot("2026-08-14", "17:30")

    assert await cache.get_slots("2026-08-14", 2) == ["18:00", "19:30"]
    assert await cache.get_slots("2026-08-14", 4) == ["19:30"]
    assert await cache.get_slots("2026-08-15", 2) == ["18:00"], "other dates untouched"


async def test_eviction_does_not_extend_the_staleness_window(cache: Cache) -> None:
    """Rewriting a key with a fresh TTL would let a busy date stay cached
    indefinitely, one booking at a time — the entry must keep its original
    expiry."""
    await cache.put_slots("2026-08-14", 2, ["17:30", "18:00"])
    before = await cache._client.ttl("luma:slots:2026-08-14:2")

    await cache.drop_slot("2026-08-14", "17:30")
    after = await cache._client.ttl("luma:slots:2026-08-14:2")

    assert after <= before, "the entry was given a longer life by being edited"


async def test_a_cancellation_clears_the_date(cache: Cache) -> None:
    """A cancellation frees a table we cannot identify from the response, so
    the honest move is to drop the date and re-probe."""
    await cache.put_slots("2026-08-14", 2, ["18:00"])
    await cache.put_slots("2026-08-14", 4, ["19:30"])

    await cache.drop_date("2026-08-14")

    assert await cache.get_slots("2026-08-14", 2) is None
    assert await cache.get_slots("2026-08-14", 4) is None


async def test_a_disabled_cache_is_always_a_miss() -> None:
    """With no REDIS_URL every method must behave as though nothing is cached,
    so the agent runs unchanged."""
    c = Cache(None)
    assert not c.enabled
    assert await c.claim_booking("k", {}) is None
    assert await c.get_slots("2026-08-14", 2) is None
    await c.put_slots("2026-08-14", 2, ["18:00"])
    await c.drop_slot("2026-08-14", "18:00")
    await c.drop_date("2026-08-14")
    await c.aclose()
