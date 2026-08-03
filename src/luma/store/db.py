"""Database access for the call record.

Writes are deliberately fire-and-forget from the agent's point of view. A call
must never stall or fail because the analytics database is slow -- the caller is
waiting in real time, and losing a transcript row is a far smaller problem than
a two-second silence. Everything that must not be lost is written through to the
reservation API, which the agent *does* wait on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base, Call, Handoff, ToolCall, Turn

logger = logging.getLogger("luma.store")


class CallStore:
    """Persists the conversation. Degrades to a no-op if the database is down."""

    def __init__(self, dsn: str | None, *, echo: bool = False) -> None:
        self._enabled = bool(dsn)
        self._pending: set[asyncio.Task[Any]] = set()
        # Turns, tool calls and handoffs all carry a foreign key to `calls`.
        # Every write is fired in the background, so without a gate a turn can
        # reach the database before the call row it belongs to -- real Postgres
        # rejects that outright, which is precisely the bug a foreign key is
        # there to catch. Child writes wait on this before running.
        self._call_ready: dict[str, asyncio.Task[Any]] = {}
        self._is_sqlite = False
        if not self._enabled:
            return
        # SQLite uses StaticPool, which rejects pool sizing arguments outright,
        # so the tuning is applied only to a real server dialect.
        self._is_sqlite = dsn.startswith("sqlite")
        options: dict[str, Any] = {"echo": echo}
        if not self._is_sqlite:
            options.update(
                pool_pre_ping=True,  # a recycled connection must not fail a live call
                pool_size=5,
                max_overflow=5,
            )
        self._engine = create_async_engine(dsn, **options)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def create_schema(self) -> None:
        """Create tables directly. SQLite only.

        Deliberately refuses to touch a server database. Once Alembic owns the
        schema, letting the application also run `create_all` guarantees drift:
        `create_all` builds whatever the models currently say and then skips
        every existing table, so a column added in a later migration is created
        on a fresh database and silently missing on an upgraded one -- and
        `alembic check` still reports clean because it compares models to
        metadata, not to what actually ran.

        SQLite here is dev and test only, where the file is disposable.
        """
        if not self._enabled or not self._is_sqlite:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def aclose(self) -> None:
        if not self._enabled:
            return
        await self._drain(timeout=5)
        self._call_ready.clear()
        await self._engine.dispose()

    async def _drain(self, *, timeout: float) -> None:
        """Wait for queued writes, but never block indefinitely on them.

        Re-checked in a loop because a write can enqueue while we wait -- the
        gated child tasks only start once their parent lands.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while self._pending:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.warning("gave up waiting on %d queued writes", len(self._pending))
                return
            await asyncio.wait(set(self._pending), timeout=remaining)

    # ------------------------------------------------------------- internals

    def _spawn(self, coro: Any, *, after: str | None = None) -> None:
        """Run a write in the background, swallowing failures with a log line.

        Held in a set because asyncio only keeps a weak reference to tasks, and
        a garbage-collected task is silently cancelled mid-write.

        `after` names a call whose row must exist first.
        """
        task = asyncio.create_task(self._guard(coro, after))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _guard(self, coro: Any, after: str | None = None) -> None:
        try:
            if after is not None:
                parent = self._call_ready.get(after)
                if parent is not None:
                    await asyncio.shield(parent)
            await coro
        except Exception:  # analytics must never take the call down
            logger.exception("call store write failed")

    @contextlib.asynccontextmanager
    async def session(self):
        async with self._session() as s:
            yield s

    # ---------------------------------------------------------------- writes

    def call_started(self, **fields: Any) -> None:
        """Registered as the gate every other write for this call waits on."""
        if not self._enabled:
            return
        call_id = fields["id"]
        task = asyncio.create_task(self._call_started(**fields))
        self._call_ready[call_id] = task
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _call_started(self, **fields: Any) -> None:
        async with self._session() as s:
            s.add(Call(**fields))
            await s.commit()

    def record_turn(self, **fields: Any) -> None:
        if self._enabled:
            self._spawn(self._record_turn(**fields), after=fields.get("call_id"))

    async def _record_turn(self, **fields: Any) -> None:
        async with self._session() as s:
            s.add(Turn(**fields))
            await s.commit()

    def record_tool_call(self, **fields: Any) -> None:
        if self._enabled:
            self._spawn(self._record_tool_call(**fields), after=fields.get("call_id"))

    async def _record_tool_call(self, **fields: Any) -> None:
        async with self._session() as s:
            s.add(ToolCall(**fields))
            await s.commit()

    def record_handoff(self, **fields: Any) -> None:
        if self._enabled:
            self._spawn(self._record_handoff(**fields), after=fields.get("call_id"))

    async def _record_handoff(self, **fields: Any) -> None:
        async with self._session() as s:
            s.add(Handoff(**fields))
            await s.commit()

    async def call_ended(self, call_id: str, **fields: Any) -> None:
        """Awaited, unlike the rest: it is the last write of the call, and the
        process may exit immediately afterwards."""
        if not self._enabled:
            return
        try:
            # Drain first, and do NOT remove the gate. An earlier version popped
            # it here, which quietly destroyed every turn: the turn tasks had
            # been created but not yet started, so when they finally ran they
            # found no parent to wait on, raced the call insert and died on the
            # foreign key. Whatever is still queued is also what these summary
            # figures are meant to describe, so waiting is correct anyway.
            await self._drain(timeout=5)
            async with self._session() as s:
                call = await s.get(Call, call_id)
                if call is None:
                    return
                for key, value in fields.items():
                    setattr(call, key, value)
                await s.commit()
        except Exception:
            logger.exception("failed to close out call %s", call_id)
