"""The call store, against a real database.

Both tests here exist because of races that only a real foreign key catches.
Writes are fired in the background so a call never waits on analytics, and that
concurrency is exactly what makes ordering easy to get wrong: SQLite with
foreign keys off would have accepted every one of these silently.

Runs against DATABASE_URL if set, otherwise an in-process SQLite with foreign
keys enforced, so the ordering guarantees are still exercised in CI.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine

from luma.store import CallStore
from luma.store.models import Call, Handoff, ToolCall, Turn

pytestmark = pytest.mark.asyncio


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection, _record) -> None:
    """SQLite ignores foreign keys unless asked. Without this the ordering bug
    these tests exist for would pass locally and fail only in production."""
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@pytest_asyncio.fixture
async def store():
    dsn = os.getenv("DATABASE_URL") or "sqlite+aiosqlite:///:memory:"
    s = CallStore(dsn)
    await s.create_schema()
    async with s.session() as session:
        for model in (Turn, ToolCall, Handoff, Call):
            for row in (await session.execute(select(model))).scalars():
                await session.delete(row)
        await session.commit()
    yield s
    await s.aclose()


async def _count(store: CallStore, model) -> int:
    async with store.session() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_child_writes_wait_for_the_call_row(store: CallStore) -> None:
    """Turns are fired before the call row has committed.

    Every child table has a foreign key to `calls`, so without an ordering gate
    the first turns lose the race and are rejected. They must all survive.
    """
    store.call_started(id="c1", caller_name="Daniel")
    store.record_turn(call_id="c1", role="caller", text="one")
    store.record_turn(call_id="c1", role="agent", text="two", e2e_ms=2100.5)
    store.record_tool_call(call_id="c1", tool="check_availability", status="available")
    store.record_handoff(call_id="c1", reason="too large", summary="wants 12")

    await store.call_ended("c1", turn_count=2, outcome="booked")

    assert await _count(store, Call) == 1
    assert await _count(store, Turn) == 2, "a turn was lost to the foreign key race"
    assert await _count(store, ToolCall) == 1
    assert await _count(store, Handoff) == 1


async def test_call_ended_does_not_strand_queued_writes(store: CallStore) -> None:
    """`call_ended` used to remove the gate the queued turns were waiting on.

    They had been created but not yet started, so they woke to find no parent,
    raced the call insert and died -- every single turn of the call, silently,
    while the summary row committed happily. It has to drain, not discard.
    """
    store.call_started(id="c2", caller_name="Ana")
    for i in range(5):
        store.record_turn(call_id="c2", role="agent", text=f"turn {i}")

    await store.call_ended("c2", turn_count=5, outcome="booked")

    assert await _count(store, Turn) == 5

    async with store.session() as s:
        call = await s.get(Call, "c2")
        assert call is not None and call.turn_count == 5


async def test_a_disabled_store_is_a_no_op() -> None:
    """With no DATABASE_URL the agent must behave exactly as it does without
    this layer, so it can be adopted piecemeal."""
    s = CallStore(None)
    assert not s.enabled
    s.call_started(id="x")
    s.record_turn(call_id="x", role="agent", text="hi")
    await s.call_ended("x", outcome="none")
    await s.aclose()
