"""The properties that must hold no matter what the language model does.

Every test here drives the real tool layer against the real mock API with no
model in the loop, so they are fast, deterministic, and runnable without an LLM
key. The model's *use* of these tools is measured separately in eval/.
"""

from __future__ import annotations

import pytest

from luma.agent import LumaAgent
from luma.api_client import booking_idempotency_key

pytestmark = pytest.mark.asyncio

BOOKED = {"name": "Jordan Lee", "phone": "310-555-0199"}


async def _book(agent: LumaAgent, date: str, time: str, size: int, **over) -> dict:
    """Check availability then create, the way the agent is supposed to."""
    await agent.check_availability(None, date, time, size)
    payload = {**BOOKED, **over}
    kwargs = dict(
        name=payload["name"],
        phone=payload["phone"],
        date=date,
        time=time,
        party_size=size,
        caller_confirmed=True,
    )
    return await agent.create_reservation(None, **kwargs)


# ------------------------------------------------------------- availability


async def test_available_slot_is_reported_available(agent: LumaAgent) -> None:
    result = await agent.check_availability(None, "2026-08-14", "18:00", 4)
    assert result["status"] == "available"


async def test_unavailable_slot_returns_only_api_alternatives(agent: LumaAgent) -> None:
    """T2. The alternatives must come from the API, never from the model."""
    result = await agent.check_availability(None, "2026-08-14", "18:30", 4)
    assert result["status"] == "unavailable"
    assert [a["time"] for a in result["alternatives"]] == ["17:30", "18:00", "19:30"]


async def test_time_outside_the_service_grid_is_not_invented(agent: LumaAgent) -> None:
    """The README advertises 17:00 but the API rejects it. We must not pretend."""
    result = await agent.check_availability(None, "2026-08-14", "17:00", 2)
    assert result["status"] == "not_a_bookable_slot"
    assert result["reason"] == "time_not_a_seating"
    assert "5:30 PM" in result["times_we_seat"]


async def test_unknown_date_blames_the_date_not_the_time(agent: LumaAgent) -> None:
    """A bookable time on an unbookable date must not produce "we don't seat at
    six" -- the caller then hears six offered back as an alternative."""
    result = await agent.check_availability(None, "2026-09-01", "18:00", 2)
    assert result["status"] == "not_a_bookable_slot"
    assert result["reason"] == "date_not_bookable"
    assert result["bookable_dates"]
    assert "times_we_seat" not in result, "must not list times for an impossible date"


async def test_no_alternatives_tells_the_model_not_to_suggest_times(agent: LumaAgent) -> None:
    """2026-08-16 caps every slot at 4, so a party of 6 has no options at all.
    Without an explicit instruction the model recites the service grid as
    though those times were free."""
    await agent.check_availability(None, "2026-08-16", "19:00", 6)  # burns the seeded 503
    result = await agent.check_availability(None, "2026-08-16", "19:00", 6)
    assert result["status"] == "unavailable"
    assert result["alternatives"] == []
    assert "Do NOT suggest specific times" in result["instruction"]


async def test_transient_failure_is_retried_exactly_once_and_succeeds(agent: LumaAgent) -> None:
    """T6. The API 503s the first availability call for 2026-08-16."""
    result = await agent.check_availability(None, "2026-08-16", "18:00", 2)
    assert result["status"] == "available", "one retry should have recovered this"

    calls = [c for c in agent._api._latency.api_calls if c["path"] == "/availability"]
    assert [c["status"] for c in calls] == [503, 200]
    assert calls[-1]["attempts"] == 2, "exactly one retry, not a retry storm"


async def test_list_availability_returns_only_genuinely_free_times(agent: LumaAgent) -> None:
    """"What have you got?" is the commonest opening line, and answering it one
    slot at a time is unusable. 2026-08-14 seats 8/4/0/2/8/6."""
    result = await agent.list_availability(None, "2026-08-14", 4)
    assert result["status"] == "available_times"
    assert [t["time"] for t in result["times"]] == ["17:30", "18:00", "19:30", "20:00"]
    assert "18:30" not in [t["time"] for t in result["times"]], "0 seats"
    assert "19:00" not in [t["time"] for t in result["times"]], "only 2 seats"


async def test_list_availability_primes_the_write_gate(agent: LumaAgent) -> None:
    """Times it confirmed are usable for booking without a redundant re-check."""
    await agent.list_availability(None, "2026-08-14", 4)
    result = await agent.create_reservation(
        None, **BOOKED, date="2026-08-14", time="19:30", party_size=4, caller_confirmed=True
    )
    assert result["status"] == "created"


async def test_list_availability_says_so_when_nothing_fits(agent: LumaAgent) -> None:
    """Every slot on 2026-08-16 caps at 4, so a party of 5 fits nowhere. This
    also exercises the seeded 503: one of the six concurrent probes hits it and
    must recover on its single retry rather than poisoning the whole listing."""
    result = await agent.list_availability(None, "2026-08-16", 5)
    assert result["status"] == "nothing_available"
    assert "Do NOT suggest times" in result["instruction"]


async def test_list_availability_on_an_unbookable_date(agent: LumaAgent) -> None:
    result = await agent.list_availability(None, "2026-09-01", 2)
    assert result["status"] == "not_a_bookable_slot"
    assert result["reason"] == "date_not_bookable"


# --------------------------------------------------------- write guardrails


async def test_create_is_refused_without_an_availability_check(agent: LumaAgent) -> None:
    """No booking may rest on availability the model imagined."""
    result = await agent.create_reservation(
        None, **BOOKED, date="2026-08-14", time="18:00", party_size=4, caller_confirmed=True
    )
    assert result["status"] == "availability_not_verified"


async def test_create_is_refused_without_confirmation(agent: LumaAgent) -> None:
    await agent.check_availability(None, "2026-08-14", "18:00", 4)
    result = await agent.create_reservation(
        None, **BOOKED, date="2026-08-14", time="18:00", party_size=4, caller_confirmed=False
    )
    assert result["status"] == "confirmation_required"


async def test_a_corrected_party_size_invalidates_the_earlier_check(agent: LumaAgent) -> None:
    """T3. Availability was confirmed for two; the caller then said four.
    The check for two must not authorise a booking for four."""
    await agent.check_availability(None, "2026-08-15", "18:30", 2)
    result = await agent.create_reservation(
        None, **BOOKED, date="2026-08-15", time="18:30", party_size=4, caller_confirmed=True
    )
    assert result["status"] == "availability_not_verified"

    assert (await _book(agent, "2026-08-15", "18:30", 4))["status"] == "created"


async def test_oversized_party_hands_off_without_touching_the_api(agent: LumaAgent) -> None:
    before = len(agent._api._latency.api_calls)
    result = await agent.check_availability(None, "2026-08-14", "19:00", 12)
    assert result["status"] == "needs_human_handoff"
    assert len(agent._api._latency.api_calls) == before, "no wasted call on an impossible party"


async def test_unparseable_argument_asks_the_caller_rather_than_calling_the_api(
    agent: LumaAgent,
) -> None:
    result = await agent.check_availability(None, "sometime next week", "6 PM", 2)
    assert result["status"] == "invalid_arguments"
    assert result["field"] == "date"


# ------------------------------------------------------ duplicate prevention


async def test_repeated_create_within_a_call_writes_once(agent: LumaAgent) -> None:
    """T7, layer 1: the in-call memo answers without any network call."""
    first = await _book(agent, "2026-08-14", "20:00", 2)
    assert first["status"] == "created"

    before = len(agent._api._latency.api_calls)
    second = await _book(agent, "2026-08-14", "20:00", 2)
    assert second["status"] == "already_created"
    assert second["reservation"]["confirmation_code"] == first["reservation"]["confirmation_code"]
    assert len(agent._api._latency.api_calls) == before + 1, "only the availability re-check"


async def test_same_caller_booking_the_same_slot_again_is_caught_by_search(
    agent: LumaAgent, settings
) -> None:
    """Layer 2: a *new* call, so the in-call memo is empty. The pre-write
    search still finds the existing table."""
    from luma.api_client import ReservationApi
    from luma.obs import JsonLogger, LatencyBook
    from luma.state import CallState

    first = await _book(agent, "2026-08-14", "20:00", 2)
    assert first["status"] == "created"

    logger = JsonLogger("test-2", log_dir=None)
    api = ReservationApi(settings, logger, LatencyBook())
    second_call = LumaAgent(api=api, state=CallState(session_id="test-2"), logger=logger)
    try:
        result = await _book(second_call, "2026-08-14", "20:00", 2)
        assert result["status"] == "duplicate_reservation_exists"
        assert result["reservation"]["confirmation_code"] == first["reservation"]["confirmation_code"]
    finally:
        await api.aclose()


async def test_idempotency_key_is_derived_from_the_booking_not_the_attempt() -> None:
    a = booking_idempotency_key("Jordan Lee", "+13105550199", "2026-08-14", "18:00", 4)
    b = booking_idempotency_key("jordan lee ", "+13105550199", "2026-08-14", "18:00", 4)
    c = booking_idempotency_key("Jordan Lee", "+13105550199", "2026-08-14", "18:00", 5)
    assert a == b, "the same booking must always produce the same key"
    assert a != c, "a different booking must never reuse a key"


async def test_capacity_is_consumed_exactly_once(agent: LumaAgent) -> None:
    """The strongest duplicate check: what the restaurant's tables actually say."""
    await _book(agent, "2026-08-14", "17:30", 4)
    await _book(agent, "2026-08-14", "17:30", 4)  # deduped
    result = await agent._api.check_availability("2026-08-14", "17:30", 1)
    assert result.data["remaining_capacity"] == 4, "8 seats minus one party of 4"


# ------------------------------------------------------- modify and cancel


async def test_modify_requires_a_reservation_the_api_showed_us(agent: LumaAgent) -> None:
    """Guards against a hallucinated or misheard reservation id."""
    result = await agent.modify_reservation(
        None, reservation_id="res_made_up", caller_confirmed=True, time="19:30"
    )
    assert result["status"] == "unknown_reservation"


async def test_find_modify_flow(agent: LumaAgent) -> None:
    """T4."""
    found = await agent.find_reservation(None, confirmation_code="luma 4821")
    assert found["status"] == "found"
    rid = found["reservations"][0]["reservation_id"]

    unconfirmed = await agent.modify_reservation(
        None, reservation_id=rid, caller_confirmed=False, time="19:30", party_size=4
    )
    assert unconfirmed["status"] == "confirmation_required"

    result = await agent.modify_reservation(
        None, reservation_id=rid, caller_confirmed=True, time="19:30", party_size=4
    )
    assert result["status"] == "modified"
    assert result["reservation"]["time"] == "19:30"
    assert result["reservation"]["party_size"] == 4


async def test_find_by_phone_as_the_caller_says_it(agent: LumaAgent) -> None:
    """The seed record is +13105550147; a caller says "310-555-0147"."""
    found = await agent.find_reservation(None, phone="310-555-0147")
    assert found["status"] == "found"
    assert found["reservations"][0]["confirmation_code"] == "LUMA-4821"


async def test_find_cancel_flow_and_repeat_cancel(agent: LumaAgent) -> None:
    """T5. A second cancel must not corrupt capacity."""
    found = await agent.find_reservation(None, confirmation_code="LUMA-4821")
    rid = found["reservations"][0]["reservation_id"]

    assert (await agent.cancel_reservation(None, rid, caller_confirmed=False))[
        "status"
    ] == "confirmation_required"

    first = await agent.cancel_reservation(None, rid, caller_confirmed=True)
    assert first["status"] == "cancelled"

    await agent.cancel_reservation(None, rid, caller_confirmed=True)
    freed = await agent._api.check_availability("2026-08-14", "18:00", 1)
    assert freed.data["remaining_capacity"] == 6, "party of 2 returned to the pool once"


async def test_missing_search_criteria_asks_instead_of_failing(agent: LumaAgent) -> None:
    result = await agent.find_reservation(None)
    assert result["status"] == "invalid_arguments"


async def test_unknown_booking_is_reported_not_invented(agent: LumaAgent) -> None:
    result = await agent.find_reservation(None, confirmation_code="LUMA-0000")
    assert result["status"] == "not_found"


# ------------------------------------------------------------------ handoff


async def test_handoff_preserves_the_summary_and_collected_details(agent: LumaAgent) -> None:
    agent._state.record_turn("caller", "I need a table for twelve on Friday.")
    await agent.check_availability(None, "2026-08-14", "19:00", 12)

    result = await agent.transfer_to_human(None, reason="party_size_exceeds_standard")
    assert result["status"] == "handoff_queued"
    assert result["handoff_id"]

    summary = agent._state.conversation_summary("party_size_exceeds_standard")
    assert "2026-08-14" in summary
    assert "table for twelve" in summary
    assert "check_availability" in summary


