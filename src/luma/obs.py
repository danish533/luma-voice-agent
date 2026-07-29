"""Structured logging and latency measurement.

Every event is one JSON object on one line, tagged with the session id, so a
whole call can be replayed with `jq` and latency percentiles can be computed
without a metrics backend. In production this is the same shape you would ship
to Datadog/Loki; only the sink changes.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

_REDACT_KEYS = {"phone", "customer_phone", "caller_phone"}


def redact_phone(value: str | None) -> str | None:
    """Keep the last four digits only. Enough to debug, not enough to identify."""
    if not value:
        return value
    digits = [c for c in value if c.isdigit()]
    if len(digits) <= 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + "".join(digits[-4:])


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (redact_phone(v) if k in _REDACT_KEYS and isinstance(v, str) else _scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


class JsonLogger:
    """Line-delimited JSON logger writing to stderr and, optionally, a file."""

    def __init__(self, session_id: str, log_dir: str | None = None, level: str = "INFO") -> None:
        self.session_id = session_id
        self._lock = threading.Lock()
        self._file = None
        self._level = level
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            self._file = open(os.path.join(log_dir, f"{session_id}.jsonl"), "a", encoding="utf-8")

    def log(self, event: str, **fields: Any) -> None:
        record = {
            "ts": time.time(),
            "session_id": self.session_id,
            "event": event,
            **_scrub(fields),
        }
        line = json.dumps(record, default=str)
        with self._lock:
            print(line, file=sys.stderr, flush=True)
            if self._file:
                self._file.write(line + "\n")
                self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None


@contextmanager
def timed() -> Iterator[dict[str, float]]:
    """Measure wall-clock milliseconds around a block.

    >>> with timed() as t: ...
    >>> t["ms"]
    """
    span: dict[str, float] = {}
    start = time.perf_counter()
    try:
        yield span
    finally:
        span["ms"] = round((time.perf_counter() - start) * 1000, 2)


@dataclass
class TurnLatency:
    """One conversational turn, assembled from LiveKit's per-component metrics.

    LiveKit emits EOU/LLM/TTS metrics separately but tags them with a shared
    `speech_id`, so they can be joined into the single number that actually
    matters to a caller: silence -> first audio.
    """

    speech_id: str
    eou_delay_ms: float | None = None
    llm_ttft_ms: float | None = None
    tts_ttfb_ms: float | None = None

    @property
    def end_of_speech_to_first_audio_ms(self) -> float | None:
        parts = [self.eou_delay_ms, self.llm_ttft_ms, self.tts_ttfb_ms]
        if any(p is None for p in parts):
            return None
        return round(sum(p for p in parts if p is not None), 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "speech_id": self.speech_id,
            "eou_delay_ms": self.eou_delay_ms,
            "llm_ttft_ms": self.llm_ttft_ms,
            "tts_ttfb_ms": self.tts_ttfb_ms,
            "end_of_speech_to_first_audio_ms": self.end_of_speech_to_first_audio_ms,
        }


@dataclass
class LatencyBook:
    """Collects turn latencies and API latencies for the evaluation report."""

    turns: dict[str, TurnLatency] = field(default_factory=dict)
    api_calls: list[dict[str, Any]] = field(default_factory=list)

    def turn(self, speech_id: str) -> TurnLatency:
        return self.turns.setdefault(speech_id, TurnLatency(speech_id=speech_id))

    def record_api(self, *, method: str, path: str, status: int, ms: float, attempts: int) -> None:
        self.api_calls.append(
            {"method": method, "path": path, "status": status, "ms": ms, "attempts": attempts}
        )

    @staticmethod
    def _pct(values: list[float], pct: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        # Nearest-rank percentile: unambiguous and stable on the small samples
        # a seven-scenario suite produces.
        idx = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered) + 0.5) - 1))
        return round(ordered[idx], 2)

    def summary(self) -> dict[str, Any]:
        voice = [
            t.end_of_speech_to_first_audio_ms
            for t in self.turns.values()
            if t.end_of_speech_to_first_audio_ms is not None
        ]
        api = [c["ms"] for c in self.api_calls]
        return {
            "voice_turns": len(voice),
            "voice_p50_ms": self._pct(voice, 50),
            "voice_p95_ms": self._pct(voice, 95),
            "api_calls": len(api),
            "api_p50_ms": self._pct(api, 50),
            "api_p95_ms": self._pct(api, 95),
        }
