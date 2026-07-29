"""Normalisation is the seam between what a caller says and what the API takes.

These run without a network, a microphone or an API key.
"""

from __future__ import annotations

from datetime import date

import pytest

from luma.normalize import (
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

TODAY = date(2026, 7, 29)


@pytest.mark.parametrize(
    "spoken",
    [
        "310-555-0147",
        "(310) 555 0147",
        "310 555 0147",
        "+1 310 555 0147",
        "3105550147",
        "13105550147",
        "+13105550147",
        "three one zero five five five zero one four seven",
    ],
)
def test_every_spelling_reaches_the_same_e164(spoken: str) -> None:
    """The seeded reservation is stored as +13105550147 and the API matches on
    the exact string, so anything less than E.164 silently finds nothing."""
    assert normalize_phone(spoken) == "+13105550147"


@pytest.mark.parametrize("bad", ["", "   ", "12345", "not a number"])
def test_unusable_phone_numbers_raise(bad: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        normalize_phone(bad)
    assert exc.value.field == "phone"


def test_phone_variants_are_ordered_most_likely_first() -> None:
    assert phone_variants("310-555-0147") == ["+13105550147", "13105550147", "3105550147"]


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("2026-08-14", "2026-08-14"),
        ("August 14", "2026-08-14"),
        ("aug 14 2026", "2026-08-14"),
        ("Friday, August 14th", "2026-08-14"),
        ("8/14", "2026-08-14"),
        ("8/14/2026", "2026-08-14"),
        ("today", "2026-07-29"),
        ("tomorrow", "2026-07-30"),
    ],
)
def test_date_forms(spoken: str, expected: str) -> None:
    assert normalize_date(spoken, today=TODAY) == expected


def test_bare_month_day_rolls_into_next_year() -> None:
    """A caller in July asking for "January 3" means the coming January."""
    assert normalize_date("January 3", today=TODAY) == "2027-01-03"


@pytest.mark.parametrize("bad", ["", "sometime next week", "February 30"])
def test_unparseable_dates_raise(bad: str) -> None:
    with pytest.raises(NormalizationError):
        normalize_date(bad, today=TODAY)


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("6 PM", "18:00"),
        ("6:30 PM", "18:30"),
        ("7:30pm", "19:30"),  # no word boundary between digit and "pm"
        ("5:30 p.m.", "17:30"),
        ("18:00", "18:00"),
        ("1830", "18:30"),
        ("six thirty PM", "18:30"),
        ("around 7 pm", "19:00"),
        ("12 PM", "12:00"),
        ("12 AM", "00:00"),
        ("7", "19:00"),  # dinner service: a bare hour is the evening one
    ],
)
def test_time_forms(spoken: str, expected: str) -> None:
    assert normalize_time(spoken) == expected


@pytest.mark.parametrize("bad", ["", "quarter past", "banana"])
def test_ambiguous_times_raise_rather_than_guess(bad: str) -> None:
    """Booking the wrong time is worse than asking the caller to repeat."""
    with pytest.raises(NormalizationError):
        normalize_time(bad)


def test_time_is_not_snapped_to_the_slot_grid() -> None:
    """6:45 stays 6:45 so the API can reject it and we can offer real slots,
    rather than quietly booking a time nobody asked for."""
    assert normalize_time("6:45 PM") == "18:45"


@pytest.mark.parametrize("spoken", ["luma 4821", "LUMA4821", "LUMA-4821", "  luma-4821 "])
def test_confirmation_code_forms(spoken: str) -> None:
    assert normalize_confirmation_code(spoken) == "LUMA-4821"


@pytest.mark.parametrize("spoken,expected", [(4, 4), ("4", 4), ("four", 4), ("four people", 4)])
def test_party_size_forms(spoken: object, expected: int) -> None:
    assert normalize_party_size(spoken) == expected


def test_readback_helpers_are_speakable() -> None:
    assert spoken_date("2026-08-14") == "Friday, August 14"
    assert spoken_time("19:30") == "7:30 PM"
    assert spoken_time("18:00") == "6 PM"
