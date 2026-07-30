"""Stand-ins for when Postgres or Redis is not configured.

Null objects rather than `if self._cache is not None` at every call site: the
agent has one code path whether or not the production layer is deployed, so
"works without Redis" is structural instead of something twelve branches have
to remember.

They live beside the real implementations so a new method on `Cache` or
`CallStore` is obviously missing here too.
"""

from __future__ import annotations

from typing import Any


class NullCache:
    """Every read is a miss; every write is discarded."""

    enabled = False

    async def get_slots(self, *_: Any) -> None:
        return None

    async def put_slots(self, *_: Any) -> None:
        return None

    async def drop_slot(self, *_: Any) -> None:
        return None

    async def drop_date(self, *_: Any) -> None:
        return None

    async def claim_booking(self, *_: Any) -> None:
        """None means "you won the key", so the booking proceeds exactly as it
        would with no shared idempotency at all."""
        return None

    async def store_booking(self, *_: Any) -> None:
        return None

    async def ping(self) -> bool:
        return False

    async def aclose(self) -> None:
        return None


class NullStore:
    """Accepts every write and keeps none."""

    enabled = False

    def call_started(self, **_: Any) -> None:
        return None

    def record_turn(self, **_: Any) -> None:
        return None

    def record_tool_call(self, **_: Any) -> None:
        return None

    def record_handoff(self, **_: Any) -> None:
        return None

    async def call_ended(self, *_: Any, **__: Any) -> None:
        return None

    async def create_schema(self) -> None:
        return None

    async def aclose(self) -> None:
        return None
