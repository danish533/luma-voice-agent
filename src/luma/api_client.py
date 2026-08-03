"""Async HTTP client for the Luma Bistro reservation API.

Responsibilities kept here rather than in the agent:
  * one bounded retry, and only for faults that are actually transient;
  * deterministic idempotency keys, so a retry can never double-book;
  * a latency measurement and a structured log line for every request.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any

import httpx

from . import metrics
from .config import Settings
from .obs import JsonLogger, LatencyBook, timed

# 5xx means "the upstream is briefly unhappy, ask again". 4xx means "you asked
# for something impossible" -- retrying that just burns the caller's patience.
RETRYABLE_STATUS = {500, 502, 503, 504}


@dataclass
class ApiResult:
    ok: bool
    status: int
    data: Any
    error_code: str | None = None
    error_detail: Any = None
    latency_ms: float = 0.0
    attempts: int = 1

    @property
    def transient(self) -> bool:
        return not self.ok and (self.status in RETRYABLE_STATUS or self.status == 0)


def booking_idempotency_key(
    name: str, phone: str, date: str, time: str, party_size: int
) -> str:
    """Derive a stable key from the booking itself.

    A key minted per *attempt* (say, a fresh uuid4) makes the header useless:
    every retry creates another reservation. Deriving it from the normalized
    booking fields means a retry, a stuttered tool call, or a caller repeating
    themselves all collapse onto the same server-side record. Changing any
    detail correctly produces a different booking.

    Note the supplied API keys its cache on the header alone and ignores the
    body, so the key must never be reused across different bookings.
    """
    payload = f"{name.strip().lower()}|{phone}|{date}|{time}|{int(party_size)}"
    return "luma-" + hashlib.sha256(payload.encode()).hexdigest()[:32]


class ReservationApi:
    def __init__(
        self,
        settings: Settings,
        logger: JsonLogger,
        latency: LatencyBook | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._log = logger
        self._latency = latency or LatencyBook()
        self._client = client or httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=settings.api_timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retry: bool = True,
    ) -> ApiResult:
        max_attempts = 1 + (self._settings.api_max_retries if retry else 0)
        attempt = 0
        result: ApiResult | None = None

        while attempt < max_attempts:
            attempt += 1
            with timed() as span:
                try:
                    response = await self._client.request(
                        method, path, params=params, json=json, headers=headers
                    )
                    result = _to_result(response, span_ms=0.0, attempts=attempt)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    result = ApiResult(
                        ok=False,
                        status=0,
                        data=None,
                        error_code="NETWORK_ERROR",
                        error_detail=str(exc),
                        attempts=attempt,
                    )
            result.latency_ms = span["ms"]

            self._log.log(
                "api_call",
                method=method,
                path=path,
                status=result.status,
                attempt=attempt,
                latency_ms=result.latency_ms,
                error_code=result.error_code,
            )
            self._latency.record_api(
                method=method,
                path=path,
                status=result.status,
                ms=result.latency_ms,
                attempts=attempt,
            )
            metrics.record_api_call(
                method=method,
                path=path,
                status=result.status,
                ms=result.latency_ms,
                attempts=attempt,
            )

            if result.ok or not result.transient or attempt >= max_attempts:
                break

            await asyncio.sleep(self._retry_delay_s(result))

        assert result is not None
        return result

    def _retry_delay_s(self, result: ApiResult) -> float:
        """Honour the server's `retry_after_ms`, but never block a live call."""
        hint = 250
        if isinstance(result.error_detail, dict):
            hint = int(result.error_detail.get("retry_after_ms", hint) or hint)
        return min(hint, self._settings.api_retry_cap_ms) / 1000

    # ------------------------------------------------------------- endpoints

    async def health(self) -> ApiResult:
        return await self._request("GET", "/health")

    async def restaurant(self) -> ApiResult:
        return await self._request("GET", "/restaurant")

    async def check_availability(self, date: str, time: str, party_size: int) -> ApiResult:
        return await self._request(
            "GET",
            "/availability",
            params={"date": date, "time": time, "party_size": party_size},
        )

    async def create_reservation(
        self,
        *,
        name: str,
        phone: str,
        date: str,
        time: str,
        party_size: int,
        notes: str | None,
        idempotency_key: str,
    ) -> ApiResult:
        # Safe to retry precisely because the key is deterministic.
        return await self._request(
            "POST",
            "/reservations",
            json={
                "name": name,
                "phone": phone,
                "date": date,
                "time": time,
                "party_size": party_size,
                "notes": notes,
            },
            headers={"Idempotency-Key": idempotency_key},
        )

    async def search_reservations(
        self, *, phone: str | None = None, confirmation_code: str | None = None
    ) -> ApiResult:
        params: dict[str, Any] = {}
        if phone:
            params["phone"] = phone
        if confirmation_code:
            params["confirmation_code"] = confirmation_code
        return await self._request("GET", "/reservations/search", params=params)

    async def update_reservation(self, reservation_id: str, patch: dict[str, Any]) -> ApiResult:
        return await self._request("PATCH", f"/reservations/{reservation_id}", json=patch)

    async def cancel_reservation(self, reservation_id: str) -> ApiResult:
        # Naturally idempotent server-side: a second cancel is a no-op.
        return await self._request("POST", f"/reservations/{reservation_id}/cancel")

    async def handoff(
        self, *, reason: str, customer_phone: str | None, conversation_summary: str
    ) -> ApiResult:
        return await self._request(
            "POST",
            "/handoff",
            json={
                "reason": reason,
                "customer_phone": customer_phone,
                "conversation_summary": conversation_summary,
            },
        )

    async def reset(self) -> ApiResult:
        return await self._request("POST", "/admin/reset", retry=False)


def _to_result(response: httpx.Response, *, span_ms: float, attempts: int) -> ApiResult:
    try:
        body = response.json()
    except ValueError:
        body = response.text

    if response.is_success:
        return ApiResult(
            ok=True, status=response.status_code, data=body, latency_ms=span_ms, attempts=attempts
        )

    detail = body.get("detail") if isinstance(body, dict) else body
    code = None
    if isinstance(detail, dict):
        code = detail.get("code")
    elif isinstance(detail, list) and detail:
        # FastAPI/Pydantic validation errors arrive as a list of field errors.
        code = "VALIDATION_ERROR"
    return ApiResult(
        ok=False,
        status=response.status_code,
        data=None,
        error_code=code or f"HTTP_{response.status_code}",
        error_detail=detail,
        latency_ms=span_ms,
        attempts=attempts,
    )
