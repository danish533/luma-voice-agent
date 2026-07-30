"""Redis: idempotency that outlives the process, and a short-lived slot cache.

Two very different jobs, deliberately in one place so the difference is obvious.

**Idempotency** is a correctness mechanism. The in-process memo in `CallState`
only protects against a repeat within one call; a worker restart, or a caller
who redials and reaches a different worker, sails straight past it. Redis makes
the memo shared and durable, so the same booking cannot be written twice by two
processes.

**The availability cache** is a latency mechanism, and it is deliberately *not*
allowed near the write path. Answering "what have you got on Saturday?" from a
90-second-old snapshot is fine -- the caller is choosing, not committing. Using
that same snapshot to authorise a booking is not fine, and the availability gate
in `agent.py` still demands a fresh 200 from the API for the exact slot before
any reservation is created. A cache that can make the agent claim a table exists
is a cache that makes the agent lie.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("luma.cache")

# Long enough to cover a single call's browsing, short enough that a table
# booked by someone else does not linger on offer for long. The window is a
# staleness bound, not an optimisation: the voice worker and any other process
# hold separate views, and this is how far apart they are allowed to drift.
AVAILABILITY_TTL_S = 90

# Comfortably longer than a call, so a redial cannot slip past the memo, and
# short enough that keys do not accumulate forever.
IDEMPOTENCY_TTL_S = 24 * 3600


class Cache:
    """Redis-backed. Every method degrades to a miss if Redis is unavailable."""

    def __init__(self, url: str | None, *, client: Any = None) -> None:
        self._client = client
        self._enabled = bool(url or client)
        if url and client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(url, decode_responses=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def aclose(self) -> None:
        if self._enabled and hasattr(self._client, "aclose"):
            await self._client.aclose()

    async def ping(self) -> bool:
        if not self._enabled:
            return False
        try:
            return bool(await self._client.ping())
        except Exception:
            logger.warning("redis unreachable; running without a shared cache")
            return False

    # ---------------------------------------------------------- idempotency

    async def claim_booking(self, key: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Claim a booking key, or return the reservation already written under it.

        SET NX is the whole mechanism: exactly one caller wins the key, and
        everyone else gets the winner's record back. Checking then setting would
        leave a window between the two in which a second worker could also
        decide it was first.
        """
        if not self._enabled:
            return None
        try:
            won = await self._client.set(
                f"luma:booking:{key}", json.dumps(payload), nx=True, ex=IDEMPOTENCY_TTL_S
            )
            if won:
                return None
            existing = await self._client.get(f"luma:booking:{key}")
            return json.loads(existing) if existing else None
        except Exception:
            logger.exception("idempotency claim failed; falling back to the API's own key")
            return None

    async def store_booking(self, key: str, reservation: dict[str, Any]) -> None:
        """Replace the placeholder with the real reservation once it exists."""
        if not self._enabled:
            return
        try:
            await self._client.set(
                f"luma:booking:{key}", json.dumps(reservation), ex=IDEMPOTENCY_TTL_S
            )
        except Exception:
            logger.exception("failed to store booking under its idempotency key")

    # ------------------------------------------------------------ slot cache

    @staticmethod
    def _slot_key(date: str, party_size: int) -> str:
        return f"luma:slots:{date}:{party_size}"

    async def get_slots(self, date: str, party_size: int) -> list[str] | None:
        if not self._enabled:
            return None
        try:
            raw = await self._client.get(self._slot_key(date, party_size))
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def put_slots(self, date: str, party_size: int, times: list[str]) -> None:
        if not self._enabled:
            return
        try:
            await self._client.set(
                self._slot_key(date, party_size), json.dumps(times), ex=AVAILABILITY_TTL_S
            )
        except Exception:
            logger.exception("failed to cache availability")

    async def drop_slot(self, date: str, time: str) -> None:
        """Remove one time from every cached party size for that date.

        Surgical rather than flushing the date: a booking removes exactly the
        slot it consumed, and the remaining entries keep their original expiry
        so a write cannot extend the staleness window.
        """
        if not self._enabled:
            return
        try:
            async for key in self._client.scan_iter(match=f"luma:slots:{date}:*"):
                raw = await self._client.get(key)
                if not raw:
                    continue
                times = [t for t in json.loads(raw) if t != time]
                ttl = await self._client.ttl(key)
                if ttl and ttl > 0:
                    await self._client.set(key, json.dumps(times), ex=ttl)
        except Exception:
            logger.exception("failed to evict %s %s from the slot cache", date, time)

    async def drop_date(self, date: str) -> None:
        """Used after a cancellation, which frees a table we cannot pinpoint."""
        if not self._enabled:
            return
        try:
            async for key in self._client.scan_iter(match=f"luma:slots:{date}:*"):
                await self._client.delete(key)
        except Exception:
            logger.exception("failed to clear the slot cache for %s", date)
