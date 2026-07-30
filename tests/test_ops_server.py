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
    monkeypatch.setenv("OPS_USERNAME", "ops")
    monkeypatch.setenv("OPS_PASSWORD", "test-password")

    import importlib
    import sys
    from pathlib import Path

    ops_dir = Path(__file__).resolve().parents[1] / "ops"
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))
    import server

    importlib.reload(server)          # pick up the patched credentials
    return TestClient(server.app)


def _sign_in(client: TestClient) -> None:
    r = client.post("/login", data={"username": "ops", "password": "test-password"},
                    follow_redirects=False)
    assert r.status_code == 303, "sign-in should redirect to the console"


def test_token_endpoint_mints_a_room_scoped_jwt(client: TestClient) -> None:
    _sign_in(client)
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
    _sign_in(client)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    assert client.post("/api/token").status_code == 500


def test_state_endpoint_answers_even_with_the_reservation_api_down(client: TestClient) -> None:
    """The console has to stay up to *report* that the API is down."""
    _sign_in(client)
    body = client.get("/api/state").json()
    assert set(body) >= {"api_online", "slots", "dates", "grid", "events", "stats", "reservations"}
    assert isinstance(body["reservations"], list)


def test_index_serves_the_page_and_the_vendored_sdk(client: TestClient) -> None:
    _sign_in(client)
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="call"' in page.text, "the call button must be on the page"
    assert "/vendor/livekit-client.umd.min.js" in page.text

    sdk = client.get("/vendor/livekit-client.umd.min.js")
    assert sdk.status_code == 200, "run scripts/fetch_vendor.sh"
    assert len(sdk.content) > 100_000


# ------------------------------------------------------------------- auth


def test_customer_data_is_not_served_to_anonymous_callers(client: TestClient) -> None:
    """The console shows names, dates, party sizes and partial phone numbers,
    and /api/token mints a LiveKit room token. None of it may be reachable
    without a session."""
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/token").status_code == 401

    landing = client.get("/", follow_redirects=False)
    assert landing.status_code == 303
    assert landing.headers["location"] == "/login", "the one route a person types"


def test_a_wrong_password_is_refused(client: TestClient) -> None:
    bad = client.post("/login", data={"username": "ops", "password": "wrong"},
                      follow_redirects=False)
    assert bad.status_code == 303 and "bad=1" in bad.headers["location"]
    assert client.get("/api/state").status_code == 401, "no session was issued"


def test_a_wrong_username_is_refused(client: TestClient) -> None:
    bad = client.post("/login", data={"username": "nobody", "password": "test-password"},
                      follow_redirects=False)
    assert "bad=1" in bad.headers["location"]


def test_the_session_cookie_is_not_readable_by_javascript(client: TestClient) -> None:
    """HttpOnly means an XSS bug cannot lift the session; SameSite blocks a
    cross-site form post riding it."""
    r = client.post("/login", data={"username": "ops", "password": "test-password"},
                    follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_logging_out_ends_the_session(client: TestClient) -> None:
    _sign_in(client)
    assert client.get("/api/state").status_code == 200
    client.post("/logout", follow_redirects=False)
    assert client.get("/api/state").status_code == 401


def test_a_forged_cookie_is_rejected(client: TestClient) -> None:
    """The cookie is signed, so a hand-written one must not open a session."""
    client.cookies.set("luma_ops", "eyJ1Ijoib3BzIn0.forged.signature")
    assert client.get("/api/state").status_code == 401


# -------------------------------------------------------------- websocket


def test_the_websocket_pushes_state_and_needs_a_session(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect as WSDisconnect

    with pytest.raises(WSDisconnect):
        with client.websocket_connect("/ws"):
            pass  # closed with 1008 before any frame

    _sign_in(client)
    with client.websocket_connect("/ws") as ws:
        payload = ws.receive_json()
    assert set(payload) >= {"api_online", "grid", "events", "stats", "reservations"}
