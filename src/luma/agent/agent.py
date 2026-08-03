"""The Luma Bistro agent: seven tools over the reservation API.

Each tool reads as a pipeline -- normalise, run the guards, call the API, shape
the reply -- because the parts that used to be inlined now live in siblings:
`guards.py` holds the preconditions, `replies.py` the wording, `speech.py` the
filler. The interesting logic is the order of the guards, and it is now visible
at a glance rather than buried in two hundred lines of branching.
"""

from __future__ import annotations

import asyncio
from typing import Any

from livekit.agents import Agent, RunContext, function_tool

from .. import metrics
from ..api_client import ReservationApi, booking_idempotency_key
from ..config import MAX_STANDARD_PARTY_SIZE, SERVICE_SLOTS
from ..normalize import (
    NormalizationError,
    normalize_confirmation_code,
    normalize_date,
    normalize_party_size,
    normalize_phone,
    normalize_time,
    phone_variants,
    spoken_date,
    spoken_time,
)
from ..obs import JsonLogger
from ..prompts import system_prompt
from ..state import CallState
from ..store.null import NullCache, NullStore
from . import guards, replies
from .speech import filler


class LumaAgent(Agent):
    def __init__(
        self,
        *,
        api: ReservationApi,
        state: CallState,
        logger: JsonLogger,
        cache: Any = None,
        store: Any = None,
    ) -> None:
        super().__init__(instructions=system_prompt())
        self._api = api
        self._state = state
        self._log = logger
        # Null objects, so every code path below is the same whether or not the
        # production layer is deployed.
        self._cache = cache or NullCache()
        self._store = store or NullStore()

    # ------------------------------------------------------------- internals

    def _start(self, tool: str, arguments: dict[str, Any]) -> None:
        self._log.log("tool_call", tool=tool, arguments=arguments)

    def _finish(self, tool: str, arguments: dict, result: dict) -> dict:
        status = str(result.get("status"))
        metrics.TOOL_CALLS.labels(tool=tool, status=status).inc()
        self._state.record_tool_call(tool, arguments, result)
        self._log.log("tool_result", tool=tool, status=result.get("status"))
        reservation = result.get("reservation") or {}
        self._store.record_tool_call(
            call_id=self._state.session_id,
            tool=tool,
            arguments=arguments,
            status=str(result.get("status")),
            reservation_id=reservation.get("reservation_id"),
            confirmation_code=reservation.get("confirmation_code"),
        )
        return result

    async def _do_handoff(self, reason: str) -> dict[str, Any]:
        summary = self._state.conversation_summary(reason)
        result = await self._api.handoff(
            reason=reason,
            customer_phone=self._state.caller_phone,
            conversation_summary=summary,
        )
        if not result.ok:
            # The handoff endpoint itself failed. Say something true.
            self._log.log("handoff_failed", reason=reason, error=result.error_code)
            return {
                "status": "handoff_failed",
                "say_to_caller": (
                    "I can't reach the team right now. Please call us back in a few "
                    "minutes and we'll sort this out."
                ),
            }

        self._state.handoff = result.data
        metrics.HANDOFFS.labels(reason=reason[:40]).inc()
        self._log.log(
            "handoff_queued",
            handoff_id=result.data.get("handoff_id"),
            reason=reason,
            summary_chars=len(summary),
        )
        self._store.record_handoff(
            call_id=self._state.session_id,
            reason=reason,
            summary=summary,
            customer_phone=self._state.caller_phone,
            external_id=result.data.get("handoff_id"),
        )
        return {
            "status": "handoff_queued",
            "handoff_id": result.data.get("handoff_id"),
            "say_to_caller": (
                "I'm passing you to a colleague now, and everything you've told me "
                "goes with you, so you won't need to repeat yourself."
            ),
        }

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

    # ----------------------------------------------------------------- tools

    @function_tool
    async def check_availability(
        self, ctx: RunContext, date: str, time: str, party_size: int
    ) -> dict[str, Any]:
        """Check whether a table is free. Call this before offering any time.

        Args:
            date: The requested date, ideally as YYYY-MM-DD.
            time: The requested time, 24-hour HH:MM if you know it.
            party_size: Number of guests.
        """
        args = {"date": date, "time": time, "party_size": party_size}
        self._start("check_availability", args)

        try:
            iso_date = normalize_date(date)
            hhmm = normalize_time(time)
            size = normalize_party_size(party_size)
        except NormalizationError as exc:
            return self._finish("check_availability", args, replies.invalid(exc.field, exc.hint))

        self._state.requested_date = iso_date
        self._state.requested_time = hhmm
        self._state.party_size = size

        if refusal := guards.party_size_is_bookable(size):
            return self._finish("check_availability", args, refusal)

        async with filler(ctx):
            result = await self._api.check_availability(iso_date, hhmm, size)

        if result.ok:
            payload = result.data
            # Remembered here and nowhere else: this is the only thing that
            # authorises a later booking for these exact details.
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
            return self._finish(
                "check_availability",
                args,
                replies.unavailable(iso_date, hhmm, size, payload.get("alternatives", [])),
            )

        if result.error_code == "INVALID_SLOT":
            return self._finish(
                "check_availability",
                args,
                {"status": "not_a_bookable_slot", **replies.slot_guidance(iso_date)},
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
                        "Our booking system isn't responding. Let me pass you to a "
                        "colleague."
                    ),
                },
            )

        return self._finish(
            "check_availability", args, {"status": "error", "error_code": result.error_code}
        )

    @function_tool
    async def list_availability(
        self, ctx: RunContext, date: str, party_size: int
    ) -> dict[str, Any]:
        """List every free time on a date. Use this when the caller asks what is
        available rather than naming a specific time.

        Args:
            date: The date to look at, ideally as YYYY-MM-DD.
            party_size: Number of guests.
        """
        args = {"date": date, "party_size": party_size}
        self._start("list_availability", args)

        try:
            iso_date = normalize_date(date)
            size = normalize_party_size(party_size)
        except NormalizationError as exc:
            return self._finish("list_availability", args, replies.invalid(exc.field, exc.hint))

        self._state.requested_date = iso_date
        self._state.party_size = size

        if refusal := guards.party_size_is_bookable(size):
            return self._finish("list_availability", args, refusal)

        # Browsing may be slightly stale -- the caller is choosing, not
        # committing -- so a warm cache answers instantly. Booking may not: the
        # availability gate still demands a fresh 200 for the exact slot.
        if (cached := await self._cache.get_slots(iso_date, size)) is not None:
            self._log.log("cache_hit", scope="slots", date=iso_date, party_size=size)
            return self._finish(
                "list_availability", args, replies.slot_listing(iso_date, size, cached)
            )

        # The API has no "slots for a day" endpoint, so the grid is probed.
        # Concurrently, because six sequential round trips inside a live turn is
        # a noticeable pause. Every answer is API truth, not configuration.
        async with filler(ctx):
            results = await asyncio.gather(
                *(self._api.check_availability(iso_date, slot, size) for slot in SERVICE_SLOTS)
            )

        if all(r.error_code == "INVALID_SLOT" for r in results):
            return self._finish(
                "list_availability",
                args,
                {"status": "not_a_bookable_slot", **replies.slot_guidance(iso_date)},
            )
        if any(r.transient for r in results):
            return self._finish(
                "list_availability",
                args,
                {
                    "status": "temporarily_unavailable",
                    "next_step": "call transfer_to_human",
                    "say_to_caller": (
                        "Our booking system isn't responding. Let me pass you to a "
                        "colleague."
                    ),
                },
            )

        free: list[str] = []
        for slot, result in zip(SERVICE_SLOTS, results):
            if result.ok and result.data.get("available"):
                # Remembered, so booking one of these needs no second check.
                self._state.remember_availability(iso_date, slot, size, result.data)
                free.append(slot)

        await self._cache.put_slots(iso_date, size, free)
        return self._finish("list_availability", args, replies.slot_listing(iso_date, size, free))

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
        self._start("create_reservation", args)

        try:
            iso_date = normalize_date(date)
            hhmm = normalize_time(time)
            size = normalize_party_size(party_size)
            e164 = normalize_phone(phone)
        except NormalizationError as exc:
            return self._finish("create_reservation", args, replies.invalid(exc.field, exc.hint))

        if not name or len(name.strip()) < 2:
            return self._finish(
                "create_reservation",
                args,
                replies.invalid("name", "Could I take the name for the booking?"),
            )
        clean_name = name.strip()

        self._state.caller_name = clean_name
        self._state.caller_phone = e164
        self._state.notes = notes

        # The order is the design. See guards.py.
        if refusal := guards.party_size_is_bookable(size):
            return self._finish("create_reservation", args, refusal)
        if refusal := guards.not_already_created(self._state, iso_date, hhmm, size, e164):
            self._log.log("duplicate_prevented", layer="in_call_memo")
            metrics.DUPLICATES_PREVENTED.labels(layer="in_call_memo").inc()
            return self._finish("create_reservation", args, refusal)
        if refusal := guards.availability_was_verified(self._state, iso_date, hhmm, size):
            return self._finish("create_reservation", args, refusal)
        if refusal := guards.caller_has_confirmed(
            caller_confirmed, name=clean_name, date=iso_date, time=hhmm, size=size
        ):
            return self._finish("create_reservation", args, refusal)
        if refusal := await guards.no_existing_booking_at_that_time(
            self._api, e164, iso_date, hhmm
        ):
            self._log.log("duplicate_prevented", layer="pre_write_search")
            metrics.DUPLICATES_PREVENTED.labels(layer="pre_write_search").inc()
            return self._finish("create_reservation", args, refusal)

        # Deterministic, so a retry lands on the same record rather than making
        # a second one.
        key = booking_idempotency_key(clean_name, e164, iso_date, hhmm, size)
        if refusal := await guards.not_claimed_by_another_worker(self._cache, key):
            self._log.log("duplicate_prevented", layer="redis_idempotency")
            metrics.DUPLICATES_PREVENTED.labels(layer="redis_idempotency").inc()
            return self._finish("create_reservation", args, refusal)

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
            # Keep the collected details in step with what was actually booked:
            # they were last set by check_availability, possibly for a time the
            # caller then moved away from, and this is what a colleague sees in
            # the handoff summary.
            self._state.requested_date = iso_date
            self._state.requested_time = hhmm
            self._state.party_size = size
            self._state.created_reservations.append(record)
            self._state.known_reservation_ids.add(record["reservation_id"])
            await self._cache.store_booking(key, record)
            # Surgical: this booking took exactly this slot, so evict only that
            # time and leave every other cached entry on its own expiry.
            await self._cache.drop_slot(iso_date, hhmm)
            self._log.log(
                "reservation_created",
                reservation_id=record["reservation_id"],
                confirmation_code=record["confirmation_code"],
                idempotency_key=key,
            )
            return self._finish(
                "create_reservation", args, {"status": "created", "reservation": replies.public(record)}
            )

        if result.error_code == "SLOT_UNAVAILABLE":
            # Someone took the last table between our check and our write.
            detail = result.error_detail if isinstance(result.error_detail, dict) else {}
            return self._finish(
                "create_reservation",
                args,
                {
                    "status": "slot_unavailable",
                    "alternatives": replies.alternatives(detail.get("alternatives", [])),
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
                    **replies.slot_guidance(iso_date),
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
        self._start("find_reservation", args)

        if not confirmation_code and not phone:
            return self._finish(
                "find_reservation",
                args,
                replies.invalid(
                    "search",
                    "Do you have your confirmation code, or the phone number you booked with?",
                ),
            )

        records: list[dict[str, Any]] = []
        async with filler(ctx):
            if confirmation_code:
                code = normalize_confirmation_code(confirmation_code)
                result = await self._api.search_reservations(confirmation_code=code)
                if not result.ok:
                    return self._finish(
                        "find_reservation",
                        args,
                        replies.api_failure(result, "look up that booking"),
                    )
                records = result.data.get("results", [])

            if not records and phone:
                try:
                    self._state.caller_phone = normalize_phone(phone)
                except NormalizationError as exc:
                    return self._finish(
                        "find_reservation", args, replies.invalid(exc.field, exc.hint)
                    )
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
            {"status": "found", "reservations": [replies.public(r) for r in records]},
        )

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
        self._start("modify_reservation", args)

        if refusal := guards.reservation_is_known(self._state, reservation_id):
            return self._finish("modify_reservation", args, refusal)
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
            return self._finish("modify_reservation", args, replies.invalid(exc.field, exc.hint))
        if notes is not None:
            patch["notes"] = notes

        if not patch:
            return self._finish(
                "modify_reservation",
                args,
                replies.invalid("change", "What would you like to change about the booking?"),
            )
        if patch.get("party_size") and (
            refusal := guards.party_size_is_bookable(patch["party_size"])
        ):
            return self._finish("modify_reservation", args, refusal)

        async with filler(ctx):
            result = await self._api.update_reservation(reservation_id, patch)

        if result.ok:
            record = result.data
            self._state.remember_reservations([record])
            # A move frees a table on one date and takes one on another, and the
            # response does not say which slot was released -- so both dates are
            # re-probed rather than guessed at.
            for touched in {patch.get("date"), record.get("date")}:
                if touched:
                    await self._cache.drop_date(touched)
            self._log.log(
                "reservation_modified",
                reservation_id=reservation_id,
                confirmation_code=record.get("confirmation_code"),
                patch=patch,
            )
            return self._finish(
                "modify_reservation",
                args,
                {"status": "modified", "reservation": replies.public(record)},
            )

        detail = result.error_detail if isinstance(result.error_detail, dict) else {}
        if result.error_code == "SLOT_UNAVAILABLE":
            return self._finish(
                "modify_reservation",
                args,
                {
                    "status": "slot_unavailable",
                    "alternatives": replies.alternatives(detail.get("alternatives", [])),
                },
            )
        if result.error_code == "ALREADY_CANCELLED":
            return self._finish(
                "modify_reservation",
                args,
                {
                    "status": "already_cancelled",
                    "say_to_caller": "That booking was already cancelled. Shall I make a new one?",
                },
            )
        if result.error_code == "NOT_FOUND":
            return self._finish("modify_reservation", args, {"status": "not_found"})
        if result.error_code == "INVALID_SLOT":
            return self._finish(
                "modify_reservation",
                args,
                {"status": "not_a_bookable_slot", **replies.slot_guidance(patch.get("date"))},
            )
        return self._finish(
            "modify_reservation", args, replies.api_failure(result, "change that booking")
        )

    @function_tool
    async def cancel_reservation(
        self, ctx: RunContext, reservation_id: str, caller_confirmed: bool
    ) -> dict[str, Any]:
        """Cancel a booking. Find it first and confirm explicitly -- this cannot
        be undone.

        Args:
            reservation_id: The id from find_reservation.
            caller_confirmed: True only if the caller has just said yes to
                cancelling this specific booking.
        """
        args = {"reservation_id": reservation_id, "caller_confirmed": caller_confirmed}
        self._start("cancel_reservation", args)

        if refusal := guards.reservation_is_known(self._state, reservation_id):
            return self._finish("cancel_reservation", args, refusal)
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

        async with filler(ctx):
            result = await self._api.cancel_reservation(reservation_id)

        if result.ok:
            # A cancellation frees a table we cannot identify from the response,
            # so the honest move is to drop the date and re-probe.
            if freed := result.data.get("date"):
                await self._cache.drop_date(freed)
            self._log.log(
                "reservation_cancelled",
                reservation_id=reservation_id,
                confirmation_code=result.data.get("confirmation_code"),
            )
            return self._finish(
                "cancel_reservation",
                args,
                {"status": "cancelled", "reservation": replies.public(result.data)},
            )
        if result.error_code == "NOT_FOUND":
            return self._finish("cancel_reservation", args, {"status": "not_found"})
        return self._finish(
            "cancel_reservation", args, replies.api_failure(result, "cancel that booking")
        )

    @function_tool
    async def transfer_to_human(self, ctx: RunContext, reason: str) -> dict[str, Any]:
        """Hand the call to a colleague, carrying the summary and everything
        collected so far.

        Args:
            reason: Why the transfer is needed, in a few words.
        """
        self._start("transfer_to_human", {"reason": reason})
        result = await self._do_handoff(reason)
        return self._finish("transfer_to_human", {"reason": reason}, result)


__all__ = ["LumaAgent", "MAX_STANDARD_PARTY_SIZE"]
