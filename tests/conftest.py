from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from dotenv import load_dotenv

from luma.agent import LumaAgent
from luma.api_client import ReservationApi
from luma.config import Settings
from luma.obs import JsonLogger, LatencyBook
from luma.state import CallState

# Read .env so the suite targets the same API the agent does. Without this the
# default port applies, nothing answers, and a third of the tests skip -- which
# reads as a pass and hides real breakage.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

API_URL = os.getenv("RESERVATION_API_URL", "http://127.0.0.1:8000")


def pytest_collection_modifyitems(session, config, items) -> None:
    """Stop once, with one clear line, if the mock API is not up.

    Every guardrail test needs it. Letting them fail individually produces
    twenty-five identical tracebacks for a single cause, which buries any real
    failure sitting among them.
    """
    if not any("test_guardrails" in item.nodeid for item in items):
        return
    try:
        httpx.get(f"{API_URL}/health", timeout=2.0).raise_for_status()
    except Exception:
        pytest.exit(
            f"\nThe mock reservation API is not running at {API_URL}.\n"
            f"Start it with `make api`, then re-run.\n"
            f"(Refusing to skip: a silent skip here would hide every write "
            f"guardrail these tests exist to protect.)\n",
            returncode=1,
        )


@pytest.fixture
def settings() -> Settings:
    os.environ.setdefault("RESERVATION_API_URL", API_URL)
    return Settings.from_env()


@pytest_asyncio.fixture
async def agent(settings: Settings):
    """A fresh agent against a freshly reset API, as the scenarios require."""
    logger = JsonLogger("test", log_dir=None)
    api = ReservationApi(settings, logger, LatencyBook())

    reset = await api.reset()
    if not reset.ok:
        await api.aclose()
        pytest.fail(f"mock API unreachable at {settings.api_base_url}")

    state = CallState(session_id="test")
    yield LumaAgent(api=api, state=state, logger=logger)
    await api.aclose()
