"""The preconditions that hold regardless of what the language model decides.

Each guard returns ``None`` to allow the write, or a refusal dict to stop it.
Extracted from the tools because they *are* the product: a prompt is guidance,
a precondition is a guarantee, and previously they were interleaved with
plumbing in a two-hundred-line function where neither could be read.

Order matters and is asserted by the tests:

  1. party size          -- an impossible party costs no API call
  2. in-call memo        -- a repeat is answered from memory, no network
  3. availability gate   -- nothing is booked on availability nobody checked
  4. read-back           -- only once the table is known to exist
  5. pre-write search    -- a booking from an earlier call
  6. shared idempotency  -- another worker, or this one before a restart
"""

from __future__ import annotations

from typing import Any

from ..config import MAX_STANDARD_PARTY_SIZE
from ..normalize import spoken_date, spoken_time
from ..state import CallState
from . import replies


def party_size_is_bookable(size: int) -> dict[str, Any] | None:
    """Parties over the house limit go to a person, without spending a call."""
    if size <= MAX_STANDARD_PARTY_SIZE:
        return None
    return {
        "status": "needs_human_handoff",
        "reason": "party_size_exceeds_standard",
        "say_to_caller": (
            f"A party of {size} needs a colleague's help. Let me transfer you."
        ),
    }


def not_already_created(
    state: CallState, date: str, time: str, size: int, phone: str
) -> dict[str, Any] | None:
    """This exact booking was already made during this call.

    Checked before the availability gate and answered from memory: a repeat
    costs no network call, and cannot be derailed by availability that moved
    underneath us -- which it necessarily did, since our own booking took the
    seats.
    """
    existing = state.find_created(date, time, size, phone)
    if not existing:
        return None
    return {
        "status": "already_created",
        "reservation": replies.public(existing),
        "say_to_caller": "That's already booked.",
    }


def availability_was_verified(
    state: CallState, date: str, time: str, size: int
) -> dict[str, Any] | None:
    """No booking may rest on availability the model imagined.

    Also fixes the correction case: a caller who changes the party size
    invalidates the earlier check, forcing a re-check before the write.
    """
    if state.availability_verified(date, time, size):
        return None
    return {
        "status": "availability_not_verified",
        "next_step": "call check_availability for these exact details first",
    }


def caller_has_confirmed(
    confirmed: bool, *, name: str, date: str, time: str, size: int
) -> dict[str, Any] | None:
    """The read-back. Reached only once the table is known to exist, so the
    agent never reads back a booking that cannot happen."""
    if confirmed:
        return None
    return {
        "status": "confirmation_required",
        "say_to_caller": (
            f"So that's {name}, {spoken_date(date)} at {spoken_time(time)}, "
            f"for {size}. Shall I book it?"
        ),
        "next_step": (
            "Read that back, WAIT for the caller to answer, then call again with "
            "the same details and caller_confirmed=true."
        ),
    }


def reservation_is_known(state: CallState, reservation_id: str) -> dict[str, Any] | None:
    """Refuse an id the API has not shown us this call.

    Stops a hallucinated or misheard id from mutating a stranger's booking.
    """
    if reservation_id in state.known_reservation_ids:
        return None
    return {
        "status": "unknown_reservation",
        "next_step": "call find_reservation first and use the id it returns",
    }


async def no_existing_booking_at_that_time(
    api: Any, phone: str, date: str, time: str
) -> dict[str, Any] | None:
    """The same caller may already hold this table from an earlier call.

    A single E.164 lookup, not the multi-spelling fallback find_reservation
    uses: our own writes are always E.164, and this sits on the critical path
    of every booking, where the common case is that nothing is found.
    """
    prior = await api.search_reservations(phone=phone)
    for record in prior.data.get("results", []) if prior.ok else []:
        if (
            record.get("date") == date
            and record.get("time") == time
            and record.get("status") == "confirmed"
        ):
            return {
                "status": "duplicate_reservation_exists",
                "reservation": replies.public(record),
                "say_to_caller": (
                    "You already have a table booked at that time. Would you like "
                    "me to change it instead?"
                ),
            }
    return None


async def not_claimed_by_another_worker(cache: Any, key: str) -> dict[str, Any] | None:
    """The last line of defence, and the only one that spans processes.

    The in-call memo sees one call and the pre-write search can miss a booking
    stored under a different spelling. A redial landing on another worker, or a
    retry after a restart, gets past both. SET NX means exactly one caller
    writes and the rest are handed the winner's record.
    """
    claimed = await cache.claim_booking(key, {"pending": True})
    if claimed and claimed.get("confirmation_code"):
        return {"status": "already_created", "reservation": replies.public(claimed)}
    return None
