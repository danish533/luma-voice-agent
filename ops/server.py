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
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from luma.config import SERVICE_SLOTS, Settings  # noqa: E402

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
    logs = sorted(_log_dir().glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


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
    if event == "agent_turn" and record.get("interrupted"):
        return _row(ts, "warning", "Caller interrupted the agent", "barge-in")
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


def read_session(path: Path) -> tuple[list[dict], dict[str, Any]]:
    rows: list[dict] = []
    stats = {
        "created": 0, "duplicates_prevented": 0, "retries": 0,
        "handoffs": 0, "interruptions": 0, "tool_calls": 0,
    }
    latencies: list[float] = []

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

        if row := classify(record):
            rows.append(row)

    stats["turn_p50"] = _pct(latencies, 50)
    stats["turn_p95"] = _pct(latencies, 95)
    stats["turns"] = len(latencies)
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

    session = newest_session()
    events, stats = read_session(session) if session else ([], {})
    return {
        "api_online": health,
        "api_url": settings.api_base_url,
        "session": session.stem if session else None,
        "slots": list(SERVICE_SLOTS),
        "dates": list(POLLED_DATES),
        "grid": grid,
        "events": events,
        "stats": stats,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (Path(__file__).resolve().parent / "index.html").read_text()


if __name__ == "__main__":
    print(f"Ops console  http://127.0.0.1:8100   (watching {settings.api_base_url})")
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
