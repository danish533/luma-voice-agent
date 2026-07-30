"""Turn what a caller *says* into what the API *accepts*.

Speech-to-text produces "three ten, five five five, oh one four seven" or
"310-555-0147"; "next Friday"; "six thirty PM". The reservation API accepts
E.164, ISO dates and 24-hour times, and rejects everything else with an opaque
422. Normalising here -- before the tool ever reaches the network -- is what
keeps tool calls reliable, and it is unit-testable without a microphone.
"""

from __future__ import annotations

import re
from datetime import date as _date
from datetime import datetime

from .config import RESTAURANT_TZ

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_NUMBER_WORDS = {
    "zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}


class NormalizationError(ValueError):
    """Raised when caller input cannot be coerced into an API-valid value.

    Carries a `field` and a caller-facing `hint` so the agent can ask a precise
    follow-up question instead of a generic "sorry, say that again".
    """

    def __init__(self, field: str, hint: str) -> None:
        super().__init__(hint)
        self.field = field
        self.hint = hint


def today_in_restaurant_tz() -> _date:
    return datetime.now(RESTAURANT_TZ).date()


def normalize_phone(raw: str) -> str:
    """Return an E.164 string such as ``+13105550147``.

    The supplied API stores phone numbers verbatim after stripping non-digits,
    and its seed record is E.164. A search for the same number typed as
    ``310-555-0147`` therefore returns zero results. Every phone number that
    crosses the tool boundary is forced into E.164 so writes and reads agree.
    """
    if not raw or not str(raw).strip():
        raise NormalizationError("phone", "I didn't catch a phone number.")

    text = str(raw).lower()
    # Spell out "five five five" style dictation before stripping punctuation.
    tokens = re.split(r"[\s,.\-()]+", text)
    rebuilt = "".join(
        str(_NUMBER_WORDS[t]) if t in _NUMBER_WORDS else t for t in tokens if t
    )

    has_plus = rebuilt.strip().startswith("+")
    digits = re.sub(r"\D", "", rebuilt)

    if has_plus:
        if not 8 <= len(digits) <= 15:
            raise NormalizationError("phone", "That number doesn't look complete.")
        if digits.startswith("1"):
            _check_nanp(digits[1:])
        return "+" + digits
    if len(digits) == 10:  # North American number without country code
        _check_nanp(digits)
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        _check_nanp(digits[1:])
        return "+" + digits
    if 8 <= len(digits) <= 15:
        return "+" + digits
    raise NormalizationError(
        "phone", "I need a ten digit phone number, area code first."
    )


def _check_nanp(local: str) -> None:
    """Reject North American numbers that cannot exist.

    Without this a misheard digit becomes a stored phone number: a caller
    saying "one two one two, four oh three, double oh seven" produced
    +11212403007, whose area code is "1212". The booking is then unreachable
    and, worse, unfindable — every lookup is by phone number.

    NANP rules: neither the area code nor the exchange may begin with 0 or 1.
    """
    if len(local) != 10:
        return  # not a NANP number; leave it to the length checks above
    area, exchange = local[:3], local[3:6]
    if area[0] in "01" or exchange[0] in "01":
        raise NormalizationError(
            "phone",
            "That doesn't look like a valid number — could you give me the "
            "ten digits again, starting with the area code?",
        )


def phone_variants(raw: str) -> list[str]:
    """Every spelling of a number the API might have stored, most likely first.

    Our own writes are always E.164, but the seeded and hand-created records may
    not be, so search falls back to the bare-digit form.
    """
    e164 = normalize_phone(raw)
    digits = e164.lstrip("+")
    variants = [e164, digits]
    if digits.startswith("1") and len(digits) == 11:
        variants.append(digits[1:])
    seen: list[str] = []
    for v in variants:
        if v not in seen:
            seen.append(v)
    return seen


def normalize_date(raw: str, *, today: _date | None = None) -> str:
    """Return ``YYYY-MM-DD`` in the restaurant's timezone.

    Accepts ISO, "August 14", "Aug 14 2026", "8/14", "today", "tomorrow".
    A bare month/day with no year resolves to the next occurrence, so a caller
    in December asking for "January 3" gets next year.
    """
    if not raw or not str(raw).strip():
        raise NormalizationError("date", "I didn't catch which date you'd like.")

    today = today or today_in_restaurant_tz()
    text = str(raw).strip().lower()

    if text in {"today", "tonight"}:
        return today.isoformat()
    if text == "tomorrow":
        return _date.fromordinal(today.toordinal() + 1).isoformat()

    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        y, mo, d = (int(g) for g in m.groups())
        return _safe_date(y, mo, d)

    # "8/14" or "8/14/2026"
    if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", text):
        mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = _resolve_year(y, mo, d, today)
        return _safe_date(year, mo, d)

    # "august 14", "14 august", optionally with a weekday and/or year
    month = next((v for k, v in _MONTHS.items() if re.search(rf"\b{k}\b", text)), None)
    if month is not None:
        day_match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text)
        if day_match:
            day = int(day_match.group(1))
            year_match = re.search(r"\b(20\d{2})\b", text)
            year = _resolve_year(year_match.group(1) if year_match else None, month, day, today)
            return _safe_date(year, month, day)

    raise NormalizationError(
        "date", "I need the date as a month and day, like August fourteenth."
    )


def _resolve_year(raw_year: str | None, month: int, day: int, today: _date) -> int:
    if raw_year:
        year = int(raw_year)
        return year + 2000 if year < 100 else year
    candidate = _date(today.year, month, min(day, 28))
    return today.year if candidate >= _date(today.year, today.month, today.day) else today.year + 1


def _safe_date(year: int, month: int, day: int) -> str:
    try:
        return _date(year, month, day).isoformat()
    except ValueError as exc:
        raise NormalizationError("date", "That date doesn't exist on the calendar.") from exc


def normalize_time(raw: str) -> str:
    """Return 24-hour ``HH:MM``.

    Accepts "18:00", "6 PM", "6:30pm", "1830", "six thirty PM". Times are *not*
    snapped to the 30-minute grid: a request for 6:45 stays 6:45 so the API can
    reject it honestly and the agent can offer real neighbouring slots, rather
    than silently booking a different time than the caller asked for.
    """
    if not raw or not str(raw).strip():
        raise NormalizationError("time", "I didn't catch a time.")

    text = str(raw).strip().lower().replace(".", "")
    text = re.sub(r"\b(about|around|maybe|please|ish|roughly|like)\b", " ", text)

    # No leading \b: in "7:30pm" the digit and the "p" are both word characters,
    # so there is no boundary between them. A lookbehind for a letter keeps this
    # from firing inside words such as "spam".
    meridiem = None
    if re.search(r"(?<![a-z])p\s?m\b", text):
        meridiem = "pm"
    elif re.search(r"(?<![a-z])a\s?m\b", text):
        meridiem = "am"
    text = re.sub(r"(?<![a-z])[ap]\s?m\b", "", text).strip()

    for word, value in _NUMBER_WORDS.items():
        text = re.sub(rf"\b{word}\b", str(value), text)
    text = re.sub(r"\bthirty\b", "30", text)
    text = re.sub(r"\bfifteen\b", "15", text)
    text = re.sub(r"\bforty ?5\b", "45", text)
    text = re.sub(r"\bo'?clock\b", "", text).strip()

    if m := re.fullmatch(r"(\d{1,2})[:\s](\d{2})", text):
        hour, minute = int(m.group(1)), int(m.group(2))
    elif m := re.fullmatch(r"(\d{3,4})", text):
        hour, minute = int(m.group(1)[:-2]), int(m.group(1)[-2:])
    elif m := re.fullmatch(r"(\d{1,2})", text):
        hour, minute = int(m.group(1)), 0
    else:
        raise NormalizationError("time", "I need a time, like six thirty PM.")

    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif meridiem is None and 1 <= hour <= 10:
        # Luma Bistro only serves dinner, so a bare "6" is unambiguously 18:00.
        hour += 12

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise NormalizationError("time", "That isn't a valid time of day.")
    return f"{hour:02d}:{minute:02d}"


def normalize_party_size(raw: int | str) -> int:
    if isinstance(raw, str):
        text = raw.strip().lower()
        for word, value in _NUMBER_WORDS.items():
            text = re.sub(rf"\b{word}\b", str(value), text)
        m = re.search(r"\d+", text)
        if not m:
            raise NormalizationError("party_size", "How many people will be dining?")
        raw = int(m.group())
    size = int(raw)
    if size < 1:
        raise NormalizationError("party_size", "A booking needs at least one guest.")
    return size


def normalize_confirmation_code(raw: str) -> str:
    """Return ``LUMA-XXXX``. STT renders the code as "luma 4821" or "LUMA4821"."""
    if not raw or not str(raw).strip():
        raise NormalizationError("confirmation_code", "What's the confirmation code?")
    text = re.sub(r"[^A-Za-z0-9]", "", str(raw)).upper()
    if text.startswith("LUMA"):
        suffix = text[4:]
        if suffix:
            return f"LUMA-{suffix}"
    return str(raw).strip().upper()


def spoken_time(hhmm: str) -> str:
    """Render ``19:30`` as ``7:30 PM`` for read-back."""
    hour, minute = (int(p) for p in hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}" if minute else f"{display_hour} {suffix}"


def spoken_date(iso: str) -> str:
    """Render ``2026-08-14`` as ``Friday, August 14``."""
    parsed = _date.fromisoformat(iso)
    return parsed.strftime("%A, %B %-d")


# NATO alphabet, because "C as in Charlie" survives a phone line and a lone
# spoken "C" does not -- B, C, D, E, G, P, T, V and Z are near-identical over
# narrowband audio.
_NATO = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo",
    "F": "Foxtrot", "G": "Golf", "H": "Hotel", "I": "India", "J": "Juliet",
    "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November", "O": "Oscar",
    "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango",
    "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee",
    "Z": "Zulu",
}


def spoken_code(code: str) -> str:
    """Render ``LUMA-CDCF`` as something a caller can actually write down.

    Read as a word, "LUMA-CDCF" comes out of TTS as an unintelligible mumble --
    callers ask for it two or three times and still get it wrong. Every
    reservation code ends up being spelled out, so spell it out here rather than
    hoping the model decides to.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
    if not cleaned:
        return ""
    prefix, suffix = cleaned[:4], cleaned[4:]
    parts = [prefix.capitalize()] if prefix == "LUMA" else [
        ", ".join(_NATO.get(c, c) for c in prefix)
    ]
    if suffix:
        parts.append(", ".join(_NATO.get(c, c) if c.isalpha() else c for c in suffix))
    spelled = " — ".join(parts)
    # A complete sentence, not a bare fragment. Handed the code alone, the model
    # tacked it onto the end of a summary with no lead-in -- "...party of four,
    # phone 1-212-403-007. Luma, 2, 1, Bravo, 8" -- which sounds like noise
    # rather than something to write down.
    return f"Your confirmation code is {spelled}."
