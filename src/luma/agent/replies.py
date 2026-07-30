"""How a tool result is shaped for the model.

Every tool returns a small dict whose ``status`` says what happened and, where
useful, what to do next. Failures are values, not exceptions: an exception in a
voice agent surfaces to the caller as dead air, whereas
``{"status": "slot_unavailable", "alternatives": [...]}`` gives the model
something to say.

Kept apart from the tools themselves so the wording a caller eventually hears
can be read, reviewed and changed without picking through control flow.
"""

from __future__ import annotations

from typing import Any

from ..config import BOOKABLE_DATES, SERVICE_SLOTS
from ..normalize import spoken_code, spoken_date, spoken_time


def invalid(field: str, hint: str) -> dict[str, Any]:
    """A caller-facing hint, not "invalid argument".

    `hint` comes from the normaliser, so the agent can ask for exactly the
    detail it could not read -- "I need a ten digit number, area code first".
    """
    return {"status": "invalid_arguments", "field": field, "say_to_caller": hint}


def public(record: dict[str, Any]) -> dict[str, Any]:
    """Trim an API record to what the model needs in order to speak about it."""
    code = record.get("confirmation_code")
    return {
        "reservation_id": record.get("reservation_id"),
        "confirmation_code": code,
        # Read this out, not the raw code: TTS turns "LUMA-CDCF" into mush, and
        # callers ask for it two or three times and still write it down wrong.
        "say_the_code_like_this": spoken_code(code) if code else None,
        "name": record.get("name"),
        "date": record.get("date"),
        "time": record.get("time"),
        "spoken": (
            f"{spoken_date(record['date'])} at {spoken_time(record['time'])}"
            if record.get("date") and record.get("time")
            else None
        ),
        "party_size": record.get("party_size"),
        "notes": record.get("notes"),
        "status": record.get("status"),
    }


def alternatives(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Straight from the API. The model must offer these and nothing else."""
    return [
        {"date": a["date"], "time": a["time"], "spoken": spoken_time(a["time"])} for a in raw
    ]


def slot_listing(date: str, size: int, times: list[str]) -> dict[str, Any]:
    """A day's free times, whether freshly probed or served from cache."""
    if not times:
        return {
            "status": "nothing_available",
            "date": date,
            "party_size": size,
            "instruction": (
                f"Nothing on {spoken_date(date)} can seat {size}. Say so and offer "
                "another date. Do NOT suggest times."
            ),
        }
    return {
        "status": "available_times",
        "date": date,
        "spoken_date": spoken_date(date),
        "party_size": size,
        "times": [{"time": t, "spoken": spoken_time(t)} for t in times],
        "instruction": (
            "These are confirmed free. Read out at most three, most natural "
            "dinner times first, and let the caller pick."
        ),
    }


def unavailable(date: str, time: str, size: int, raw_alternatives: list[dict]) -> dict[str, Any]:
    """A slot that is taken, plus whatever the API offers instead."""
    options = alternatives(raw_alternatives)
    reply: dict[str, Any] = {
        "status": "unavailable",
        "date": date,
        "time": time,
        "party_size": size,
        "alternatives": options,
    }
    if not options:
        # Nothing on this date fits the party. Say so plainly. Without this the
        # model reaches back for a service grid it saw earlier in the
        # conversation and offers those times as though they were free --
        # exactly the invented availability the guards exist to prevent.
        reply["instruction"] = (
            f"No time on {spoken_date(date)} can seat {size}. Say so, and offer to "
            "try another date or a smaller party. Do NOT suggest specific times: "
            "none are available."
        )
    return reply


def slot_guidance(date: str | None) -> dict[str, Any]:
    """Explain *why* the API rejected a slot, and what to ask for instead.

    The API returns a bare 422 for both "we don't open that day" and "we don't
    seat at that minute", which are very different things to a caller. Telling
    someone "we don't seat at six" when the real problem is the date produces
    the nonsense reply "we don't seat at six; we seat at five thirty, six, ...".

    Neither branch claims a table is free. `times_we_seat` is the service grid,
    and the model is told so explicitly, because left to itself it will read the
    grid back as if it were availability.
    """
    if date and date not in BOOKABLE_DATES:
        return {
            "reason": "date_not_bookable",
            "say_to_caller": "We're not taking bookings for that date.",
            "bookable_dates": [spoken_date(d) for d in BOOKABLE_DATES],
            "instruction": (
                "Offer only these dates. Do not mention times yet -- you have not "
                "checked any."
            ),
        }
    return {
        "reason": "time_not_a_seating",
        "say_to_caller": "We don't seat at that time.",
        "times_we_seat": [spoken_time(s) for s in SERVICE_SLOTS],
        "instruction": (
            "These are the times we seat, NOT times known to be free. Ask which the "
            "caller would prefer, then call check_availability. Never say a time is "
            "available on the strength of this list."
        ),
    }


def api_failure(result: Any, action: str) -> dict[str, Any]:
    """A transient fault the client already retried, or a hard error."""
    if result.transient:
        return {
            "status": "temporarily_unavailable",
            "attempts": result.attempts,
            "next_step": "call transfer_to_human",
            "say_to_caller": (
                f"I'm having trouble reaching the system to {action}. Let me pass you "
                "to a colleague."
            ),
        }
    return {"status": "error", "error_code": result.error_code}
