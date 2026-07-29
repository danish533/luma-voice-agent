from __future__ import annotations

import os

import pytest
import pytest_asyncio

from luma.agent import LumaAgent
from luma.api_client import ReservationApi
from luma.config import Settings
from luma.obs import JsonLogger, LatencyBook
from luma.state import CallState

API_URL = os.getenv("RESERVATION_API_URL", "http://127.0.0.1:8000")


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
        pytest.skip(f"mock API not reachable at {settings.api_base_url}")

    state = CallState(session_id="test")
    yield LumaAgent(api=api, state=state, logger=logger)
    await api.aclose()
