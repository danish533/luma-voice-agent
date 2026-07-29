"""Smoke tests for the ops console's HTTP surface.

These exist because of a real escape: removing an unrelated feature also removed
a `timedelta` import that the token endpoint still used. Every module imported
fine, every other test passed, and the only symptom was a NameError at request
time -- so the demo's Start call button was dead and nothing said so.

A route is not covered by importing its module. It has to be called.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch) -> TestClient:
    # Dummy credentials: a token is signed locally, so nothing is contacted.
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APItest")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret-used-only-for-signing-32-bytes-min")

    import sys
    from pathlib import Path

    ops_dir = Path(__file__).resolve().parents[1] / "ops"
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))
    import server

    return TestClient(server.app)


def test_token_endpoint_mints_a_room_scoped_jwt(client: TestClient) -> None:
    response = client.post("/api/token")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["room"].startswith("luma-call-")
    assert body["url"] == "wss://test.livekit.cloud"

    payload = body["token"].split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    grants = claims["video"]
    assert grants["roomJoin"] is True
    assert grants["room"] == body["room"], "the token must not be valid for other rooms"
    assert claims["exp"] > claims["nbf"], "the token must actually expire"


def test_token_endpoint_refuses_without_livekit_credentials(client, monkeypatch) -> None:
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    assert client.post("/api/token").status_code == 500


def test_state_endpoint_answers_even_with_the_reservation_api_down(client: TestClient) -> None:
    """The console has to stay up to *report* that the API is down."""
    body = client.get("/api/state").json()
    assert set(body) >= {"api_online", "slots", "dates", "grid", "events", "stats", "reservations"}
    assert isinstance(body["reservations"], list)


def test_index_serves_the_page_and_the_vendored_sdk(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="call"' in page.text, "the call button must be on the page"
    assert "/vendor/livekit-client.umd.min.js" in page.text

    sdk = client.get("/vendor/livekit-client.umd.min.js")
    assert sdk.status_code == 200, "run scripts/fetch_vendor.sh"
    assert len(sdk.content) > 100_000
