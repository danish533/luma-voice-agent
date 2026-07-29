"""Per-call state.

Deliberately in-process and per-session: a phone call is a single short-lived
affair pinned to one worker, and an external store would add a network hop to
every turn for no benefit. What must survive the call -- the reservation, the
handoff summary -- is written through to the API. See ARCHITECTURE.md Q2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .obs import redact_phone


def availability_key(date: str, time: str, party_size: int) -> str:
    return f"{date}|{time}|{party_size}"


@dataclass
class CallState:
    session_id: str

    # Details gathered across turns. Overwritten freely: when a caller corrects
    # themselves mid-sentence, the newest value is simply the truth.
    caller_name: str | None = None
    caller_phone: str | None = None
    requested_date: str | None = None
    requested_time: str | None = None
    party_size: int | None = None
    notes: str | None = None

    # (date|time|party) -> the availability payload the API actually returned.
    # A reservation cannot be created for a combination absent from this map.
    verified_availability: dict[str, dict[str, Any]] = field(default_factory=dict)

    created_reservations: list[dict[str, Any]] = field(default_factory=list)
    # Reservation ids the API has shown us this call. Guards against the model
    # inventing or mistyping an id.
    known_reservation_ids: set[str] = field(default_factory=set)
    last_search_results: list[dict[str, Any]] = field(default_factory=list)

    handoff: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, str]] = field(default_factory=list)

    # -------------------------------------------------------------- mutation

    def remember_availability(self, date: str, time: str, party_size: int, payload: dict) -> None:
        self.verified_availability[availability_key(date, time, party_size)] = payload

    def availability_verified(self, date: str, time: str, party_size: int) -> bool:
        payload = self.verified_availability.get(availability_key(date, time, party_size))
        return bool(payload and payload.get("available"))

    def remember_reservations(self, records: list[dict[str, Any]]) -> None:
        self.last_search_results = records
        for record in records:
            if rid := record.get("reservation_id"):
                self.known_reservation_ids.add(rid)

    def find_created(self, date: str, time: str, party_size: int, phone: str) -> dict | None:
        """An identical booking already made during *this* call."""
        for record in self.created_reservations:
            if (
                record.get("date") == date
                and record.get("time") == time
                and record.get("party_size") == party_size
                and record.get("phone") == phone
            ):
                return record
        return None

    def record_tool_call(self, name: str, arguments: dict, result: dict) -> None:
        self.tool_calls.append(
            {"tool": name, "arguments": arguments, "status": result.get("status")}
        )

    def record_turn(self, role: str, text: str) -> None:
        if text and text.strip():
            self.transcript.append({"role": role, "text": text.strip()})

    # ------------------------------------------------------------- reporting

    def collected_details(self) -> dict[str, Any]:
        return {
            "name": self.caller_name,
            "phone": self.caller_phone,
            "date": self.requested_date,
            "time": self.requested_time,
            "party_size": self.party_size,
            "notes": self.notes,
        }

    def conversation_summary(self, reason: str, max_turns: int = 12) -> str:
        """A human-readable brief for the colleague picking up the call.

        Everything the caller already said must survive the transfer, so they
        are never asked to repeat themselves.
        """
        lines = [f"Handoff reason: {reason}", "", "Caller details collected:"]
        for label, value in (
            ("Name", self.caller_name),
            ("Phone", self.caller_phone),
            ("Date", self.requested_date),
            ("Time", self.requested_time),
            ("Party size", self.party_size),
            ("Notes", self.notes),
        ):
            lines.append(f"  - {label}: {value if value not in (None, '') else 'not provided'}")

        if self.created_reservations:
            lines.append("")
            lines.append("Reservations created during this call:")
            for record in self.created_reservations:
                lines.append(
                    f"  - {record.get('confirmation_code')} "
                    f"{record.get('date')} {record.get('time')} "
                    f"party of {record.get('party_size')}"
                )

        if self.last_search_results:
            lines.append("")
            lines.append("Existing reservations found:")
            for record in self.last_search_results:
                lines.append(
                    f"  - {record.get('confirmation_code')} "
                    f"{record.get('date')} {record.get('time')} "
                    f"party of {record.get('party_size')} ({record.get('status')})"
                )

        if self.tool_calls:
            lines.append("")
            lines.append("Actions attempted:")
            for call in self.tool_calls[-8:]:
                lines.append(f"  - {call['tool']} -> {call['status']}")

        if self.transcript:
            lines.append("")
            lines.append("Recent conversation:")
            for turn in self.transcript[-max_turns:]:
                lines.append(f"  {turn['role']}: {turn['text']}")

        return "\n".join(lines)

    def redacted(self) -> dict[str, Any]:
        details = self.collected_details()
        details["phone"] = redact_phone(details["phone"])
        return details
