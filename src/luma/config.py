"""Configuration for the Luma Bistro voice agent.

Everything that differs between local dev, CI and production is read from the
environment so the same image runs everywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

RESTAURANT_NAME = "Luma Bistro"
RESTAURANT_TZ = ZoneInfo("America/Los_Angeles")
MAX_STANDARD_PARTY_SIZE = 8
SLOT_MINUTES = 30  # table turn, from seed_data.json

# The mock API has no endpoint that lists bookable slots or dates: every unknown
# date/time is a flat 422 INVALID_SLOT with no hint attached. We therefore keep
# the service grid here, mirrored from the package's seed_data.json, and use it
# *only* to suggest what to ask for after the API has already rejected a slot.
# Availability itself is never read from this constant. See ARCHITECTURE.md Q10.
SERVICE_SLOTS: tuple[str, ...] = ("17:30", "18:00", "18:30", "19:00", "19:30", "20:00")
BOOKABLE_DATES: tuple[str, ...] = ("2026-08-14", "2026-08-15", "2026-08-16")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    """Runtime settings, resolved once at process start."""

    api_base_url: str
    api_timeout_s: float
    api_max_retries: int
    api_retry_cap_ms: int

    llm_provider: str  # "openai" | "google"
    llm_model: str
    llm_temperature: float
    # Optional second provider. An LLM failure mid-call is dead air, so this is
    # the one place a hot standby earns its complexity.
    llm_fallback_provider: str
    llm_fallback_model: str

    stt_model: str
    tts_model: str
    turn_detector: str  # "local" | "cloud"

    log_dir: str
    log_level: str

    # Both optional: with neither set the agent behaves exactly as it does on
    # main, so the production layer can be adopted piecemeal.
    database_url: str | None
    redis_url: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
        # gemini-2.5-flash is retired for new API keys, and the 3.x flash models
        # default to extended thinking, which costs 6-12s per completion -- far
        # too slow for a phone call. flash-lite measured ~650ms. See EVALUATION.md.
        default_model = "gemini-3.1-flash-lite" if provider == "google" else "gpt-4.1-mini"
        return cls(
            api_base_url=os.getenv("RESERVATION_API_URL", "http://localhost:8000").rstrip("/"),
            api_timeout_s=_env_float("RESERVATION_API_TIMEOUT_S", 5.0),
            # One retry, not more: the caller is waiting in real time and the
            # supplied API's only transient fault clears on the second attempt.
            api_max_retries=_env_int("RESERVATION_API_MAX_RETRIES", 1),
            api_retry_cap_ms=_env_int("RESERVATION_API_RETRY_CAP_MS", 1000),
            llm_provider=provider,
            llm_model=os.getenv("LLM_MODEL", default_model),
            llm_temperature=_env_float("LLM_TEMPERATURE", 0.3),
            llm_fallback_provider=os.getenv(
                "LLM_FALLBACK_PROVIDER", "google" if provider == "openai" else "openai"
            ).strip().lower(),
            # Empty by default: a fallback is only useful if its key is present,
            # and silently constructing one that 401s on every retry is worse
            # than having none.
            llm_fallback_model=os.getenv("LLM_FALLBACK_MODEL", "").strip(),
            stt_model=os.getenv("DEEPGRAM_STT_MODEL", "nova-3"),
            tts_model=os.getenv("DEEPGRAM_TTS_MODEL", "aura-2-thalia-en"),
            # Local by default: end-of-turn detection sits on the most
            # latency-sensitive path in the whole call, and running it in
            # process removes a network round trip from it. "cloud" trades that
            # round trip for worker CPU, which is the better deal at scale.
            turn_detector=os.getenv("TURN_DETECTOR", "local").strip().lower(),
            database_url=os.getenv("DATABASE_URL") or None,
            redis_url=os.getenv("REDIS_URL") or None,
            log_dir=os.getenv("LUMA_LOG_DIR", "logs"),
            log_level=os.getenv("LUMA_LOG_LEVEL", "INFO").upper(),
        )
