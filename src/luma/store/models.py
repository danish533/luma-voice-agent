"""What the agent knows that the reservation service does not.

The boundary matters more than the schema. Reservations live behind the
reservation API -- it owns the records, the capacity and the idempotency cache.
Copying them here would create a second booking system that can silently drift
from the first, and the first question anyone then asks is which one is right.

So these tables hold the *conversation*: who called, what was said, which tools
ran, how long each turn took, and what a colleague needs if the call is handed
over. Reservations appear only as an id and a confirmation code -- a pointer
back to the service that owns them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Call(Base):
    """One phone call, from dispatch to hangup."""

    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # the room name
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Last four digits only. Enough to reconcile a call with a booking, not
    # enough to identify the caller if this table leaks. See ARCHITECTURE.md Q11.
    caller_phone_last4: Mapped[str | None] = mapped_column(String(8))
    caller_name: Mapped[str | None] = mapped_column(String(120))

    llm_model: Mapped[str | None] = mapped_column(String(80))
    stt_model: Mapped[str | None] = mapped_column(String(80))
    tts_model: Mapped[str | None] = mapped_column(String(80))

    # Denormalised on purpose: "what is our p95 today" should not require
    # scanning every turn of every call.
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_p50_ms: Mapped[float | None] = mapped_column(Float)
    latency_p95_ms: Mapped[float | None] = mapped_column(Float)

    outcome: Mapped[str | None] = mapped_column(String(32))  # booked|modified|cancelled|handoff|none
    handed_off: Mapped[bool] = mapped_column(Boolean, default=False)

    turns: Mapped[list["Turn"]] = relationship(back_populates="call", cascade="all, delete-orphan")
    tool_calls: Mapped[list["ToolCall"]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_calls_started_at", "started_at"),)


class Turn(Base):
    """One conversational turn, with the latency the caller actually felt."""

    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    role: Mapped[str] = mapped_column(String(16))  # caller | agent
    text: Mapped[str] = mapped_column(Text)
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False)

    # Only on agent turns. e2e is measured, not summed: the legs overlap when
    # preemptive generation fires, and a tool turn adds a second LLM round trip
    # that no per-component metric covers.
    e2e_ms: Mapped[float | None] = mapped_column(Float)
    eou_ms: Mapped[float | None] = mapped_column(Float)
    llm_ttft_ms: Mapped[float | None] = mapped_column(Float)
    tts_ttfb_ms: Mapped[float | None] = mapped_column(Float)

    call: Mapped[Call] = relationship(back_populates="turns")


class ToolCall(Base):
    """Every tool invocation, including the ones a guardrail refused.

    The refusals are the interesting rows: a spike in `availability_not_verified`
    or `confirmation_required` means a prompt change has started pushing the
    model toward writes it should not be making.
    """

    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tool: Mapped[str] = mapped_column(String(64), index=True)
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(48), index=True)
    latency_ms: Mapped[float | None] = mapped_column(Float)

    # Pointers back to the reservation service, never a copy of the record.
    reservation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    confirmation_code: Mapped[str | None] = mapped_column(String(32), index=True)

    call: Mapped[Call] = relationship(back_populates="tool_calls")


class Handoff(Base):
    """A call passed to a person, and everything they need to pick it up."""

    __tablename__ = "handoffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    reason: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(Text)
    # The full number, not the last four: a colleague has to be able to call back.
    # This column is the reason the table needs encryption at rest in production.
    customer_phone: Mapped[str | None] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(String(24), default="queued")  # queued|claimed|closed
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    external_id: Mapped[str | None] = mapped_column(String(64))  # id from /handoff
