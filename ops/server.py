"""Read-only operations console.

Exists for one reason: to make the agent's behaviour *visible* on camera. A
transcript proves the agent said "you're booked"; this proves a record was
written, capacity dropped, a duplicate was blocked, or a 503 was recovered.

It is strictly an observer. It never writes to the reservation API and never
talks to the agent -- it reads the API's public availability endpoint and tails
the agent's own JSONL logs. Running it or killing it cannot affect a call.

    python ops/server.py            # http://127.0.0.1:8100
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from livekit import api as lk_api  # noqa: E402

from luma.config import (  # noqa: E402
    RESTAURANT_NAME,
    RESTAURANT_TZ,
    SERVICE_SLOTS,
    SLOT_MINUTES,
    Settings,
)
from luma.obs import redact_phone  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# 2026-08-16 is deliberately absent. The mock API returns its one-and-only 503
# on the first availability request for that date, and polling it here would
# consume that failure before the demo could show it. The API-failure scenario
# is observed through the event feed instead.
POLLED_DATES = ("2026-08-14", "2026-08-15")
GRID_TTL_S = 2.0
MAX_EVENTS = 400

app = FastAPI(title="Luma Bistro Ops", docs_url=None, redoc_url=None)
settings = Settings.from_env()
_grid_cache: dict[str, Any] = {"at": 0.0, "grid": {}}


def _log_dir() -> Path:
    return Path(__file__).resolve().parents[1] / settings.log_dir


def newest_session() -> Path | None:
    logs = recent_sessions(1)
    return logs[0] if logs else None


def recent_sessions(limit: int = 8) -> list[Path]:
    """Newest sessions first.

    Reservations are gathered across several calls, not just the current one:
    a caller frequently books on one call and reschedules on the next, and the
    later log holds only a `reservation_modified`. Reading the newest file
    alone made an existing booking vanish from the console the moment a second
    call started -- which looked exactly like the agent had lost it.
    """
    logs = sorted(_log_dir().glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[:limit]


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(errors="ignore").splitlines():
        if line.strip().startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


async def availability_grid(client: httpx.AsyncClient) -> dict[str, dict[str, int]]:
    """Remaining seats per slot. Cached briefly so a 1 Hz page poll does not
    become a 12 Hz load on the reservation API."""
    now = time.monotonic()
    if now - _grid_cache["at"] < GRID_TTL_S and _grid_cache["grid"]:
        return _grid_cache["grid"]

    grid: dict[str, dict[str, int]] = {}
    for date in POLLED_DATES:
        grid[date] = {}
        for slot in SERVICE_SLOTS:
            try:
                r = await client.get(
                    "/availability", params={"date": date, "time": slot, "party_size": 1}
                )
                grid[date][slot] = r.json().get("remaining_capacity") if r.is_success else None
            except httpx.HTTPError:
                grid[date][slot] = None
    _grid_cache.update(at=now, grid=grid)
    return grid


def classify(record: dict[str, Any]) -> dict[str, Any] | None:
    """Map a raw log line to a feed row. Returns None for lines not worth showing.

    `status` drives an icon and a label, never colour alone.
    """
    event = record.get("event")
    ts = record.get("ts")

    if event == "reservation_created":
        return _row(ts, "good", "Reservation created", record.get("confirmation_code", ""))
    if event == "reservation_modified":
        return _row(ts, "good", "Reservation modified", record.get("reservation_id", ""))
    if event == "reservation_cancelled":
        return _row(ts, "good", "Reservation cancelled", record.get("reservation_id", ""))
    if event == "duplicate_prevented":
        return _row(ts, "good", "Duplicate blocked", record.get("layer", ""))
    if event == "handoff_queued":
        return _row(ts, "serious", "Handed off to human", record.get("reason", ""))
    if event == "handoff_failed":
        return _row(ts, "critical", "Handoff failed", record.get("reason", ""))
    if event == "session_error":
        return _row(ts, "critical", "Session error", str(record.get("error", ""))[:90])
    if event == "false_interruption_recovered":
        return _row(ts, "warning", "False interruption, resumed", "")

    if event == "api_call":
        status = record.get("status")
        attempt = record.get("attempt", 1)
        path = record.get("path", "")
        ms = record.get("latency_ms")
        if status == 503:
            return _row(ts, "warning", f"API 503 on {path}", f"attempt {attempt}, retrying")
        if status and status >= 400:
            return _row(ts, "critical", f"API {status} on {path}", record.get("error_code") or "")
        if attempt > 1:
            return _row(ts, "good", f"Recovered on retry: {path}", f"{ms} ms, attempt {attempt}")
        return None  # healthy first-attempt calls are noise

    if event == "tool_call":
        return _row(ts, "info", f"tool: {record.get('tool')}", "")
    if event == "tool_result":
        status = record.get("status", "")
        level = "warning" if status in {
            "unavailable", "not_a_bookable_slot", "confirmation_required",
            "availability_not_verified", "temporarily_unavailable", "invalid_arguments",
            "unknown_reservation", "not_found",
        } else "info"
        return _row(ts, level, f"{record.get('tool')} → {status}", "")

    if event == "user_transcript":
        return _row(ts, "caller", "Caller", record.get("text", ""))
    if event == "agent_turn":
        text = record.get("text") or ""
        if record.get("interrupted"):
            return _row(ts, "warning", "Agent cut off by caller (barge-in)", text)
        return _row(ts, "agent", "Ava", text) if text else None
    if event == "turn_latency":
        total = record.get("end_of_speech_to_first_audio_ms")
        if total is None:
            return None
        return _row(
            ts, "latency", f"{total:.0f} ms to first audio",
            f"eou {record.get('eou_delay_ms')} · llm {record.get('llm_ttft_ms')} "
            f"· tts {record.get('tts_ttfb_ms')}",
        )
    return None


def _row(ts: float | None, level: str, label: str, detail: str) -> dict[str, Any]:
    return {"ts": ts, "level": level, "label": label, "detail": detail}


def read_reservations(rows: list[dict]) -> list[dict[str, Any]]:
    """Reservations this call touched, newest first.

    Reconstructed from the agent's log rather than the API, because the supplied
    API has no endpoint that lists reservations -- search needs a phone number
    or a code you must already know.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for record in rows:
        event = record.get("event")
        if event not in {"reservation_created", "reservation_modified", "reservation_cancelled"}:
            continue
        rid = record.get("reservation_id")
        if not rid:
            continue
        entry = by_id.setdefault(rid, {"reservation_id": rid, "history": []})
        entry["ts"] = record.get("ts")
        entry["history"].append(event.replace("reservation_", ""))
        # A code seen on any of the three events identifies the reservation;
        # a reschedule on a later call carries one but no "created".
        if record.get("confirmation_code"):
            entry["confirmation_code"] = record["confirmation_code"]
        if event == "reservation_created":
            entry["idempotency_key"] = record.get("idempotency_key")
            entry["status"] = "confirmed"
        elif event == "reservation_modified":
            entry["status"] = "confirmed"
            entry["patch"] = record.get("patch")
        else:
            entry["status"] = "cancelled"
    return sorted(by_id.values(), key=lambda e: e.get("ts") or 0, reverse=True)


async def enrich(reservations: list[dict[str, Any]], client: httpx.AsyncClient) -> None:
    """Fill in name, date, time and party size from the API, in place."""
    for entry in reservations:
        code = entry.get("confirmation_code")
        if not code:
            continue
        try:
            r = await client.get("/reservations/search", params={"confirmation_code": code})
            results = r.json().get("results", []) if r.is_success else []
        except httpx.HTTPError:
            continue
        if results:
            record = results[0]
            entry.update(
                name=record.get("name"),
                date=record.get("date"),
                time=record.get("time"),
                party_size=record.get("party_size"),
                notes=record.get("notes"),
                status=record.get("status"),
                phone=redact_phone(record.get("phone")),
            )


def read_session(path: Path) -> tuple[list[dict], dict[str, Any]]:
    rows: list[dict] = []
    stats = {
        "created": 0, "duplicates_prevented": 0, "retries": 0,
        "handoffs": 0, "interruptions": 0, "tool_calls": 0,
    }
    latencies: list[float] = []
    legs: dict[str, list[float]] = {}

    for line in path.read_text(errors="ignore").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        event = record.get("event")
        if event == "reservation_created":
            stats["created"] += 1
        elif event == "duplicate_prevented":
            stats["duplicates_prevented"] += 1
        elif event == "api_call" and record.get("attempt", 1) > 1:
            stats["retries"] += 1
        elif event == "handoff_queued":
            stats["handoffs"] += 1
        elif event == "agent_turn" and record.get("interrupted"):
            stats["interruptions"] += 1
        elif event == "tool_call":
            stats["tool_calls"] += 1
        elif event == "turn_latency":
            if (total := record.get("end_of_speech_to_first_audio_ms")) is not None:
                latencies.append(total)
            for leg, key in (("eou", "eou_delay_ms"), ("ttft", "llm_ttft_ms"),
                             ("ttfb", "tts_ttfb_ms")):
                if isinstance(record.get(key), (int, float)):
                    legs.setdefault(leg, []).append(record[key])

        if row := classify(record):
            rows.append(row)

    stats["turn_p50"] = _pct(latencies, 50)
    stats["turn_p95"] = _pct(latencies, 95)
    stats["turns"] = len(latencies)
    # Per-leg medians: the single number says how bad it is, these say which
    # part to go and fix.
    for leg, values in legs.items():
        stats[f"{leg}_p50"] = _pct(values, 50)
    return rows[-MAX_EVENTS:], stats


def _pct(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered) + 0.5) - 1))
    return round(ordered[idx])


@app.get("/api/state")
async def state() -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=settings.api_base_url, timeout=3.0) as client:
        try:
            health = (await client.get("/health")).is_success
        except httpx.HTTPError:
            health = False
        grid = await availability_grid(client) if health else {}

    sessions = recent_sessions()
    session = sessions[0] if sessions else None
    # The feed and the per-call KPIs stay scoped to the current call; only the
    # reservation list spans several, because a booking outlives the call that
    # made it.
    events, stats = read_session(session) if session else ([], {})

    reservations: list[dict[str, Any]] = []
    if sessions:
        merged: list[dict[str, Any]] = []
        for path in reversed(sessions):  # oldest first, so later calls win
            merged.extend(load_rows(path))
        reservations = read_reservations(merged)
        if reservations and health:
            async with httpx.AsyncClient(base_url=settings.api_base_url, timeout=3.0) as c:
                await enrich(reservations, c)
        # A record the API no longer recognises is a stale log line, not a
        # booking; showing a blank card is worse than showing nothing.
        reservations = [r for r in reservations if r.get("name")]

    return {
        "api_online": health,
        "api_url": settings.api_base_url,
        "session": session.stem if session else None,
        "slots": list(SERVICE_SLOTS),
        "dates": list(POLLED_DATES),
        "grid": grid,
        "events": events,
        "stats": stats,
        "reservations": reservations,
    }


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,")


@app.get("/calendar.ics")
async def calendar() -> Response:
    """A subscribable iCalendar feed of the reservations booked on this call.

    Chosen over a Google Calendar OAuth integration deliberately: iCalendar is
    the one format every calendar already speaks, it needs no tokens to store,
    refresh or leak, and a calendar outage can never affect a booking because
    nothing writes through it. In Google Calendar: Other calendars -> From URL.

    Reservations are read from the agent's log, since the supplied API cannot
    list them (see read_reservations).
    """
    session = newest_session()
    rows = (
        [
            json.loads(line)
            for line in session.read_text(errors="ignore").splitlines()
            if line.strip().startswith("{")
        ]
        if session
        else []
    )
    reservations = read_reservations(rows)
    async with httpx.AsyncClient(base_url=settings.api_base_url, timeout=3.0) as client:
        await enrich(reservations, client)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Luma Bistro//Reservations//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Luma Bistro reservations",
        f"X-WR-TIMEZONE:{RESTAURANT_TZ.key}",
    ]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for r in reservations:
        if not (r.get("date") and r.get("time")):
            continue
        start = datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M")
        end = start + timedelta(minutes=SLOT_MINUTES)
        # Floating local times with a TZID: the restaurant's clock is what
        # matters, and it is the same clock the caller was quoted.
        guest = r.get("name") or "Table"
        summary = f"{guest} (party of {r.get('party_size')})"
        description = f"Confirmation {r.get('confirmation_code')}. {r.get('notes') or 'No notes'}"
        status = "CANCELLED" if r.get("status") == "cancelled" else "CONFIRMED"
        tz = RESTAURANT_TZ.key
        lines += [
            "BEGIN:VEVENT",
            f"UID:{r['reservation_id']}@luma-bistro",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={tz}:{start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID={tz}:{end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"LOCATION:{_ics_escape(RESTAURANT_NAME)}",
            f"DESCRIPTION:{_ics_escape(description)}",
            f"STATUS:{status}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")

    return Response(
        content="\r\n".join(lines) + "\r\n",
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="luma-reservations.ics"'},
    )


@app.post("/api/token")
async def token() -> dict[str, str]:
    """Mint a short-lived token so the browser can join a fresh room.

    The API secret never leaves the server -- the page receives only a JWT
    scoped to one room. A new room per call keeps each demo's logs, metrics and
    transcript cleanly separated, since the worker names the session after the
    room it was dispatched to.
    """
    key, secret = os.getenv("LIVEKIT_API_KEY"), os.getenv("LIVEKIT_API_SECRET")
    url = os.getenv("LIVEKIT_URL")
    if not (key and secret and url):
        raise HTTPException(500, "LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set")

    room = f"luma-call-{uuid.uuid4().hex[:8]}"
    jwt = (
        lk_api.AccessToken(key, secret)
        .with_identity(f"caller-{uuid.uuid4().hex[:6]}")
        .with_name("Caller")
        .with_grants(
            lk_api.VideoGrants(
                room_join=True, room=room, can_publish=True, can_subscribe=True
            )
        )
        .with_ttl(timedelta(minutes=30))
        .to_jwt()
    )
    return {"url": url, "room": room, "token": jwt}


app.mount(
    "/vendor",
    StaticFiles(directory=Path(__file__).resolve().parent / "vendor"),
    name="vendor",
)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (Path(__file__).resolve().parent / "index.html").read_text()


if __name__ == "__main__":
    print(f"Ops console  http://127.0.0.1:8100   (watching {settings.api_base_url})")
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
