"""The Luma Bistro agent and its tools.

Each tool returns a small JSON-ish dict whose ``status`` field tells the model
what happened and, where relevant, what to do next. Failures are values, not
exceptions: an exception would surface to the caller as dead air, whereas a
``{"status": "slot_unavailable", "alternatives": [...]}`` gives the model
something useful to say.

Three preconditions are enforced here rather than in the prompt:
  1. no reservation without a *successful* availability check for those exact
     details (kills invented availability, and forces a re-check after a
     correction);
  2. no write without ``caller_confirmed``;
  3. no write against a reservation id the API has not shown us this call.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from livekit.agents import Agent, RunContext, function_tool

from .api_client import ReservationApi, booking_idempotency_key
from .config import BOOKABLE_DATES, MAX_STANDARD_PARTY_SIZE, SERVICE_SLOTS
from .normalize import (
    NormalizationError,
    normalize_confirmation_code,
    normalize_date,
    normalize_party_size,
    normalize_phone,
    normalize_time,
    phone_variants,
    spoken_code,
    spoken_date,
    spoken_time,
)
from .obs import JsonLogger
from .prompts import system_prompt
from .state import CallState


def _invalid(field: str, hint: str) -> dict[str, Any]:
    return {"status": "invalid_arguments", "field": field, "say_to_caller": hint}


# What a host actually says while they look something up. Silence during a tool
# call is the single most robotic thing a voice agent does -- a real person fills
# it without thinking. Fires only after the pause becomes noticeable, so quick
# lookups stay silent rather than gaining a pointless preamble.
_LOOKUP_FILLERS = ("Let me check that for you.", "One moment.", "Just looking now.")
_FILLER_DELAY_S = 0.7


def _filler(ctx: RunContext | None, *, delay: float = _FILLER_DELAY_S) -> Any:
    """Async context manager that covers a slow tool call with natural speech.

    Tolerates a missing context so the guardrail tests can drive tools directly
    without a live session.
    """
    if ctx is None:
        return contextlib.nullcontext()
    return ctx.with_filler(
        lambda step: _LOOKUP_FILLERS[step % len(_LOOKUP_FILLERS)],
        delay=delay,
        interval=6.0,
        max_steps=2,
    )


def _slot_guidance(date: str | None) -> dict[str, Any]:
    """Explain *why* the API rejected a slot, and what to ask for instead.

    The API returns a bare 422 for both "we don't open that day" and "we don't
    seat at that minute", which are very different things to a caller. Telling
    someone "we don't seat at six" when the real problem is the date produces
    the nonsense reply "we don't seat at six; we seat at five thirty, six, ...".
    So the two cases are separated here.

    Neither branch is a claim that a table is free. `times_we_seat` is the
    service grid, not availability; the model is told so explicitly, because
    left to itself it will happily read the grid back as if it were open slots.
    """
    if date and date not in BOOKABLE_DATES:
        return {
            "reason": "date_not_bookable",
            "say_to_caller": "We're not taking bookings for that date.",
            "bookable_dates": [spoken_date(d) for d in BOOKABLE_DATES],
            "instruction": (
                "Offer only these dates. Do not mention times yet -- you have "
                "not checked any."
            ),
        }
    return {
        "reason": "time_not_a_seating",
        "say_to_caller": "We don't seat at that time.",
        "times_we_seat": [spoken_time(s) for s in SERVICE_SLOTS],
        "instruction": (
            "These are the times we seat, NOT times known to be free. Ask which "
            "the caller would prefer, then call check_availability. Never say a "
            "time is available on the strength of this list."
        ),
    }


class LumaAgent(Agent):
    def __init__(self, *, api: ReservationApi, state: CallState, logger: JsonLogger) -> None:
        super().__init__(instructions=system_prompt())
        self._api = api
        self._state = state
        self._log = logger

    # ------------------------------------------------------------- internals

    def _finish(self, tool: str, arguments: dict, result: dict) -> dict:
        self._state.record_tool_call(tool, arguments, result)
        self._log.log("tool_result", tool=tool, status=result.get("status"))
        return result

    async def _do_handoff(self, reason: str) -> dict[str, Any]:
        summary = self._state.conversation_summary(reason)
        result = await self._api.handoff(
            reason=reason,
            customer_phone=self._state.caller_phone,
            conversation_summary=summary,
        )
        if result.ok:
            self._state.handoff = result.data
            self._log.log(
                "handoff_queued",
                handoff_id=result.data.get("handoff_id"),
                reason=reason,
                summary_chars=len(summary),
            )
            return {
                "status": "handoff_queued",
                "handoff_id": result.data.get("handoff_id"),
                "say_to_caller": (
                    "I'm passing you to a colleague now, and everything you've "
                    "told me goes with you, so you won't need to repeat yourself."
                ),
            }
        # The handoff endpoint itself failed. Say something true.
        self._log.log("handoff_failed", reason=reason, error=result.error_code)
        return {
            "status": "handoff_failed",
            "say_to_caller": (
                "I can't reach the team right now. Please call us back in a few "
                "minutes and we'll sort this out."
            ),
        }

    # ----------------------------------------------------------------- tools

    @function_tool
    async def check_availability(
        self,
        ctx: RunContext,
        date: str,
        time: str,
        party_size: int,
    ) -> dict[str, Any]:
        """Check whether a table is free. Call this before offering any time.

        Args:
            date: The requested date, ideally as YYYY-MM-DD.
            time: The requested time, 24-hour HH:MM if you know it.
            party_size: Number of guests.
        """
        args = {"date": date, "time": time, "party_size": party_size}
        self._log.log("tool_call", tool="check_availability", arguments=args)

        try:
            iso_date = normalize_date(date)
            hhmm = normalize_time(time)
            size = normalize_party_size(party_size)
        except NormalizationError as exc:
            return self._finish("check_availability", args, _invalid(exc.field, exc.hint))

        self._state.requested_date = iso_date
        self._state.requested_time = hhmm
        self._state.party_size = size

        if size > MAX_STANDARD_PARTY_SIZE:
            return self._finish(
                "check_availability",
                args,
                {
                    "status": "needs_human_handoff",
                    "reason": "party_size_exceeds_standard",
                    "say_to_caller": (
                        f"For a party of {size} I'll pass you to a colleague who "
                        "handles our large tables."
                    ),
                },
            )

        async with _filler(ctx):
            result = await self._api.check_availability(iso_date, hhmm, size)

        if result.ok:
            payload = result.data
            self._state.remember_availability(iso_date, hhmm, size, payload)
            if payload.get("available"):
                return self._finish(
                    "check_availability",
                    args,
                    {
                        "status": "available",
                        "date": iso_date,
                        "time": hhmm,
                        "party_size": size,
                        "spoken": f"{spoken_date(iso_date)} at {spoken_time(hhmm)}",
                    },
                )
            # Straight from the API. The model must offer these and nothing else.
            alternatives = [
                {"date": alt["date"], "time": alt["time"], "spoken": spoken_time(alt["time"])}
                for alt in payload.get("alternatives", [])
            ]
            unavailable: dict[str, Any] = {
                "status": "unavailable",
                "date": iso_date,
                "time": hhmm,
                "party_size": size,
                "alternatives": alternatives,
            }
            if not alternatives:
                # Nothing on this date fits the party. Say so plainly. Without
                # this the model reaches back for a service grid it saw earlier
                # in the conversation and offers those times as though they were
                # free -- exactly the invented availability we forbid.
                unavailable["instruction"] = (
                    f"No time on {spoken_date(iso_date)} can seat {size}. Say so, "
                    "and offer to try another date or a smaller party. Do NOT "
                    "suggest specific times: none are available."
                )
            return self._finish("check_availability", args, unavailable)

        if result.error_code == "INVALID_SLOT":
            return self._finish(
                "check_availability",
                args,
                {"status": "not_a_bookable_slot", **_slot_guidance(iso_date)},
            )

        if result.transient:
            # The client already retried once and it still failed. Do not retry
            # again, and do not pretend to know the answer.
            return self._finish(
                "check_availability",
                args,
                {
                    "status": "temporarily_unavailable",
                    "attempts": result.attempts,
                    "next_step": "call transfer_to_human",
                    "say_to_caller": (
                        "Our booking system isn't responding. Let me pass you to "
                        "a colleague."
                    ),
                },
            )

        return self._finish(
            "check_availability",
            args,
            {"status": "error", "error_code": result.error_code},
        )

    @function_tool
    async def list_availability(
        self,
        ctx: RunContext,
        date: str,
        party_size: int,
    ) -> dict[str, Any]:
        """List every free time on a date. Use this when the caller asks what is
        available rather than naming a specific time.

        Args:
            date: The date to look at, ideally as YYYY-MM-DD.
            party_size: Number of guests.
        """
        args = {"date": date, "party_size": party_size}
        self._log.log("tool_call", tool="list_availability", arguments=args)

        try:
            iso_date = normalize_date(date)
            size = normalize_party_size(party_size)
        except NormalizationError as exc:
            return self._finish("list_availability", args, _invalid(exc.field, exc.hint))

        self._state.requested_date = iso_date
        self._state.party_size = size

        if size > MAX_STANDARD_PARTY_SIZE:
            return self._finish(
                "list_availability",
                args,
                {"status": "needs_human_handoff", "reason": "party_size_exceeds_standard"},
            )

        # The API has no "slots for a day" endpoint, so the grid is probed. Done
        # concurrently because six sequential round trips inside a live turn is
        # a noticeable pause. Each answer is real API truth, not configuration.
        async with _filler(ctx):
            results = await asyncio.gather(
                *(self._api.check_availability(iso_date, slot, size) for slot in SERVICE_SLOTS)
            )

        if all(r.error_code == "INVALID_SLOT" for r in results):
            return self._finish(
                "list_availability",
                args,
                {"status": "not_a_bookable_slot", **_slot_guidance(iso_date)},
            )
        if any(r.transient for r in results):
            return self._finish(
                "list_availability",
                args,
                {
                    "status": "temporarily_unavailable",
                    "next_step": "call transfer_to_human",
                    "say_to_caller": (
                        "Our booking system isn't responding. Let me pass you to a colleague."
                    ),
                },
            )

        free = []
        for slot, result in zip(SERVICE_SLOTS, results):
            if result.ok and result.data.get("available"):
                # Remember it, so a booking at one of these times passes the
                # availability gate without a redundant second check.
                self._state.remember_availability(iso_date, slot, size, result.data)
                free.append({"time": slot, "spoken": spoken_time(slot)})

        if not free:
            return self._finish(
                "list_availability",
                args,
                {
                    "status": "nothing_available",
                    "date": iso_date,
                    "party_size": size,
                    "instruction": (
                        f"Nothing on {spoken_date(iso_date)} can seat {size}. Say so and "
                        "offer another date. Do NOT suggest times."
                    ),
                },
            )

        return self._finish(
            "list_availability",
            args,
            {
                "status": "available_times",
                "date": iso_date,
                "spoken_date": spoken_date(iso_date),
                "party_size": size,
                "times": free,
                "instruction": (
                    "These are confirmed free. Read out at most three, most natural "
                    "dinner times first, and let the caller pick."
                ),
            },
        )

    @function_tool
    async def create_reservation(
        self,
        ctx: RunContext,
        name: str,
        phone: str,
        date: str,
        time: str,
        party_size: int,
        caller_confirmed: bool,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Book the table. Only call this after reading the details back and
        hearing the caller agree.

        Args:
            name: Caller's full name.
            phone: Caller's phone number.
            date: Date as YYYY-MM-DD.
            time: Time as 24-hour HH:MM.
            party_size: Number of guests.
            caller_confirmed: True only if the caller has just agreed to these
                exact details, read back to them out loud.
            notes: Any special request, or null.
        """
        args = {
            "name": name,
            "date": date,
            "time": time,
            "party_size": party_size,
            "caller_confirmed": caller_confirmed,
        }
        self._log.log("tool_call", tool="create_reservation", arguments=args)

        try:
            iso_date = normalize_date(date)
            hhmm = normalize_time(time)
            size = normalize_party_size(party_size)
            e164 = normalize_phone(phone)
        except NormalizationError as exc:
            return self._finish("create_reservation", args, _invalid(exc.field, exc.hint))

        if not name or len(name.strip()) < 2:
            return self._finish(
                "create_reservation",
                args,
                _invalid("name", "Could I take the name for the booking?"),
            )
        clean_name = name.strip()

        self._state.caller_name = clean_name
        self._state.caller_phone = e164
        self._state.notes = notes

        if size > MAX_STANDARD_PARTY_SIZE:
            return self._finish(
                "create_reservation",
                args,
                {
                    "status": "needs_human_handoff",
                    "reason": "party_size_exceeds_standard",
                    "say_to_caller": (
                        f"A party of {size} needs a colleague's help. Let me "
                        "transfer you."
                    ),
                },
            )

        # Guard 1 -- we already booked exactly this during this call. Checked
        # first and answered from memory: a repeat costs no network call and
        # cannot be derailed by availability state that moved underneath us
        # (it necessarily did -- our own booking consumed the seats).
        if existing := self._state.find_created(iso_date, hhmm, size, e164):
            self._log.log("duplicate_prevented", layer="in_call_memo")
            return self._finish(
                "create_reservation",
                args,
                {
                    "status": "already_created",
                    "reservation": _public(existing),
                    "say_to_caller": "That's already booked.",
                },
            )

        # Guard 2 -- availability must have been verified for these exact
        # details. A corrected party size invalidates the earlier check.
        if not self._state.availability_verified(iso_date, hhmm, size):
            return self._finish(
                "create_reservation",
                args,
                {
                    "status": "availability_not_verified",
                    "next_step": "call check_availability for these exact details first",
                },
            )

        # Guard 3 -- the read-back. Only now, with the table known to exist.
        if not caller_confirmed:
            return self._finish(
                "create_reservation",
                args,
                {
                    "status": "confirmation_required",
                    "say_to_caller": (
                        f"So that's {clean_name}, {spoken_date(iso_date)} at "
                        f"{spoken_time(hhmm)}, for {size}. Shall I book it?"
                    ),
                    "next_step": (
                        "Read that back, WAIT for the caller to answer, then call "
                        "again with the same details and caller_confirmed=true."
                    ),
                },
            )

        # Guard 4 -- someone with this number may already hold a table at this
        # date and time from an earlier call. A single E.164 lookup, not the
        # multi-spelling fallback used by find_reservation: our own writes are
        # always E.164, and this sits on the critical path of every booking,
        # where the common case is that nothing is found.
        prior = await self._api.search_reservations(phone=e164)
        for record in (prior.data.get("results", []) if prior.ok else []):
            if (
                record.get("date") == iso_date
                and record.get("time") == hhmm
                and record.get("status") == "confirmed"
            ):
                self._log.log("duplicate_prevented", layer="pre_write_search")
                return self._finish(
                    "create_reservation",
                    args,
                    {
                        "status": "duplicate_reservation_exists",
                        "reservation": _public(record),
                        "say_to_caller": (
                            "You already have a table booked at that time. Would "
                            "you like me to change it instead?"
                        ),
                    },
                )

        # Guard 5 -- deterministic key, so a retry lands on the same record.
        key = booking_idempotency_key(clean_name, e164, iso_date, hhmm, size)
        result = await self._api.create_reservation(
            name=clean_name,
            phone=e164,
            date=iso_date,
            time=hhmm,
            party_size=size,
            notes=notes,
            idempotency_key=key,
        )

        if result.ok:
            record = result.data
            # Keep the collected details in step with what was actually booked.
            # They were last set by check_availability, which may have been for
            # a time the caller then moved away from -- and this state is what
            # the handoff summary shows a colleague.
            self._state.requested_date = iso_date
            self._state.requested_time = hhmm
            self._state.party_size = size
            self._state.created_reservations.append(record)
            self._state.known_reservation_ids.add(record["reservation_id"])
            self._log.log(
                "reservation_created",
                reservation_id=record["reservation_id"],
                confirmation_code=record["confirmation_code"],
                idempotency_key=key,
            )
            return self._finish(
                "create_reservation",
                args,
                {"status": "created", "reservation": _public(record)},
            )

        if result.error_code == "SLOT_UNAVAILABLE":
            # Someone took the last table between our check and our write.
            detail = result.error_detail if isinstance(result.error_detail, dict) else {}
            return self._finish(
                "create_reservation",
                args,
                {
                    "status": "slot_unavailable",
                    "alternatives": [
                        {"date": a["date"], "time": a["time"], "spoken": spoken_time(a["time"])}
                        for a in detail.get("alternatives", [])
                    ],
                    "say_to_caller": "That table just went. Here's what's still open.",
                },
            )

        if result.error_code in {"INVALID_SLOT", "VALIDATION_ERROR"}:
            return self._finish(
                "create_reservation",
                args,
                {
                    "status": "not_a_bookable_slot",
                    "say_to_caller": "I can't book that time.",
                    **_slot_guidance(iso_date),
                },
            )

        return self._finish(
            "create_reservation",
            args,
            {
                "status": "temporarily_unavailable" if result.transient else "error",
                "error_code": result.error_code,
                "next_step": "call transfer_to_human",
            },
        )

    @function_tool
    async def find_reservation(
        self,
        ctx: RunContext,
        confirmation_code: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        """Look up an existing booking before changing or cancelling it.

        Args:
            confirmation_code: A code like LUMA-4821, if the caller has one.
            phone: The phone number on the booking, otherwise.
        """
        args = {"confirmation_code": confirmation_code, "has_phone": bool(phone)}
        self._log.log("tool_call", tool="find_reservation", arguments=args)

        if not confirmation_code and not phone:
            return self._finish(
                "find_reservation",
                args,
                _invalid(
                    "search",
                    "Do you have your confirmation code, or the phone number you booked with?",
                ),
            )

        records: list[dict[str, Any]] = []
        if confirmation_code:
            code = normalize_confirmation_code(confirmation_code)
            result = await self._api.search_reservations(confirmation_code=code)
            if not result.ok:
                return self._finish(
                    "find_reservation", args, _api_failure(result, "look up that booking")
                )
            records = result.data.get("results", [])

        if not records and phone:
            try:
                self._state.caller_phone = normalize_phone(phone)
            except NormalizationError as exc:
                return self._finish("find_reservation", args, _invalid(exc.field, exc.hint))
            records = await self._find_existing(phone)

        self._state.remember_reservations(records)
        if not records:
            return self._finish(
                "find_reservation",
                args,
                {
                    "status": "not_found",
                    "say_to_caller": (
                        "I can't find a booking under those details. Could you "
                        "double-check the code or number?"
                    ),
                },
            )

        return self._finish(
            "find_reservation",
            args,
            {"status": "found", "reservations": [_public(r) for r in records]},
        )

    async def _find_existing(self, phone: str) -> list[dict[str, Any]]:
        """Search every spelling of the number the API might have stored."""
        try:
            candidates = phone_variants(phone)
        except NormalizationError:
            return []
        for candidate in candidates:
            result = await self._api.search_reservations(phone=candidate)
            if result.ok and result.data.get("results"):
                return result.data["results"]
        return []

    @function_tool
    async def modify_reservation(
        self,
        ctx: RunContext,
        reservation_id: str,
        caller_confirmed: bool,
        date: str | None = None,
        time: str | None = None,
        party_size: int | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Change an existing booking. Find it first, read the change back, then
        call this.

        Args:
            reservation_id: The id from find_reservation.
            caller_confirmed: True only if the caller has just agreed to the change.
            date: New date, or null to keep it.
            time: New time, or null to keep it.
            party_size: New party size, or null to keep it.
            notes: New note, or null to keep it.
        """
        args = {
            "reservation_id": reservation_id,
            "date": date,
            "time": time,
            "party_size": party_size,
            "caller_confirmed": caller_confirmed,
        }
        self._log.log("tool_call", tool="modify_reservation", arguments=args)

        if reservation_id not in self._state.known_reservation_ids:
            return self._finish(
                "modify_reservation",
                args,
                {
                    "status": "unknown_reservation",
                    "next_step": "call find_reservation first and use the id it returns",
                },
            )
        if not caller_confirmed:
            return self._finish(
                "modify_reservation",
                args,
                {
                    "status": "confirmation_required",
                    "next_step": (
                        "Read the change back, WAIT for the caller to answer, then "
                        "call again with the same details and caller_confirmed=true."
                    ),
                },
            )

        patch: dict[str, Any] = {}
        try:
            if date is not None:
                patch["date"] = normalize_date(date)
            if time is not None:
                patch["time"] = normalize_time(time)
            if party_size is not None:
                patch["party_size"] = normalize_party_size(party_size)
        except NormalizationError as exc:
            return self._finish("modify_reservation", args, _invalid(exc.field, exc.hint))
        if notes is not None:
            patch["notes"] = notes

        if not patch:
            return self._finish(
                "modify_reservation",
                args,
                _invalid("change", "What would you like to change about the booking?"),
            )

        if patch.get("party_size", 0) > MAX_STANDARD_PARTY_SIZE:
            return self._finish(
                "modify_reservation",
                args,
                {
                    "status": "needs_human_handoff",
                    "reason": "party_size_exceeds_standard",
                },
            )

        async with _filler(ctx):
            result = await self._api.update_reservation(reservation_id, patch)

        if result.ok:
            record = result.data
            self._state.remember_reservations([record])
            # Carry the code and the resulting details, not just the id: a
            # reschedule often happens on a later call than the booking, and a
            # consumer reading only this log line has no other way to identify
            # the reservation.
            self._log.log(
                "reservation_modified",
                reservation_id=reservation_id,
                confirmation_code=record.get("confirmation_code"),
                patch=patch,
            )
            return self._finish(
                "modify_reservation",
                args,
                {"status": "modified", "reservation": _public(record)},
            )

        detail = result.error_detail if isinstance(result.error_detail, dict) else {}
        if result.error_code == "SLOT_UNAVAILABLE":
            return self._finish(
                "modify_reservation",
                args,
                {
                    "status": "slot_unavailable",
                    "alternatives": [
                        {"date": a["date"], "time": a["time"], "spoken": spoken_time(a["time"])}
                        for a in detail.get("alternatives", [])
                    ],
                },
            )
        if result.error_code == "ALREADY_CANCELLED":
            return self._finish(
                "modify_reservation",
                args,
                {
                    "status": "already_cancelled",
                    "say_to_caller": (
                        "That booking was already cancelled. Shall I make a new one?"
                    ),
                },
            )
        if result.error_code == "NOT_FOUND":
            return self._finish("modify_reservation", args, {"status": "not_found"})
        if result.error_code == "INVALID_SLOT":
            return self._finish(
                "modify_reservation",
                args,
                {"status": "not_a_bookable_slot", **_slot_guidance(patch.get("date"))},
            )
        return self._finish(
            "modify_reservation", args, _api_failure(result, "change that booking")
        )

    @function_tool
    async def cancel_reservation(
        self,
        ctx: RunContext,
        reservation_id: str,
        caller_confirmed: bool,
    ) -> dict[str, Any]:
        """Cancel a booking. Find it first and confirm explicitly -- this cannot
        be undone.

        Args:
            reservation_id: The id from find_reservation.
            caller_confirmed: True only if the caller has just said yes to
                cancelling this specific booking.
        """
        args = {"reservation_id": reservation_id, "caller_confirmed": caller_confirmed}
        self._log.log("tool_call", tool="cancel_reservation", arguments=args)

        if reservation_id not in self._state.known_reservation_ids:
            return self._finish(
                "cancel_reservation",
                args,
                {
                    "status": "unknown_reservation",
                    "next_step": "call find_reservation first and use the id it returns",
                },
            )
        if not caller_confirmed:
            return self._finish(
                "cancel_reservation",
                args,
                {
                    "status": "confirmation_required",
                    "next_step": (
                        "Read the booking back, ask the caller to confirm the "
                        "cancellation, WAIT for their answer, then call again with "
                        "caller_confirmed=true."
                    ),
                },
            )

        async with _filler(ctx):
            result = await self._api.cancel_reservation(reservation_id)
        if result.ok:
            self._log.log(
                "reservation_cancelled",
                reservation_id=reservation_id,
                confirmation_code=result.data.get("confirmation_code"),
            )
            return self._finish(
                "cancel_reservation",
                args,
                {"status": "cancelled", "reservation": _public(result.data)},
            )
        if result.error_code == "NOT_FOUND":
            return self._finish("cancel_reservation", args, {"status": "not_found"})
        return self._finish(
            "cancel_reservation", args, _api_failure(result, "cancel that booking")
        )

    @function_tool
    async def transfer_to_human(self, ctx: RunContext, reason: str) -> dict[str, Any]:
        """Hand the call to a colleague, carrying the summary and everything
        collected so far.

        Args:
            reason: Why the transfer is needed, in a few words.
        """
        self._log.log("tool_call", tool="transfer_to_human", arguments={"reason": reason})
        result = await self._do_handoff(reason)
        return self._finish("transfer_to_human", {"reason": reason}, result)


def _public(record: dict[str, Any]) -> dict[str, Any]:
    """Trim an API record to what the model needs to speak about it."""
    code = record.get("confirmation_code")
    return {
        "reservation_id": record.get("reservation_id"),
        "confirmation_code": code,
        # Read this out, not the raw code: TTS turns "LUMA-CDCF" into mush.
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


def _api_failure(result: Any, action: str) -> dict[str, Any]:
    if result.transient:
        return {
            "status": "temporarily_unavailable",
            "attempts": result.attempts,
            "next_step": "call transfer_to_human",
            "say_to_caller": (
                f"I'm having trouble reaching the system to {action}. Let me pass "
                "you to a colleague."
            ),
        }
    return {"status": "error", "error_code": result.error_code}
