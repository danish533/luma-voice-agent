"""Tests for the worker entrypoint.

This file exists because of a specific bug. `await ctx.connect()` was missing,
and the result was an agent that registered with LiveKit, accepted a call,
initialised every component, wrote healthy log lines -- and left the caller
listening to silence. Nothing else in the suite could see it: the guardrail
tests never touch the room, and the evaluation harness runs in text mode where
there is no room to connect to.

So the assertions here are mostly about *order and side effects*, not return
values. A real LiveKit connection is not needed to prove that connect happens
before the session starts; it is needed only to prove that the connection then
works, which is what `scripts/smoke_call.py` is for.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from luma import worker
from luma.config import API_FAILURE_DATE, BOOKABLE_DATES, PREWARM_DATES, SERVICE_SLOTS


# --------------------------------------------------------------------- fakes


class FakeLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def log(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


class FakeSession:
    def __init__(self, trace: list[str]) -> None:
        self._trace = trace
        self.said: list[str] = []

    async def start(self, **_: Any) -> None:
        self._trace.append("session.start")

    async def say(self, text: str, **_: Any) -> None:
        self._trace.append("session.say")
        self.said.append(text)


class FakeApi:
    """Records which availability probes were made, and by whom."""

    def __init__(self, *, fail_dates: set[str] | None = None) -> None:
        self.probed: list[tuple[str, str, int]] = []
        self.health_calls = 0
        self._fail_dates = fail_dates or set()

    async def health(self) -> Any:
        self.health_calls += 1
        return _ok({"status": "ok"})

    async def check_availability(self, date: str, time: str, size: int) -> Any:
        self.probed.append((date, time, size))
        if date in self._fail_dates:
            return _fail()
        return _ok({"available": True})

    def dates_probed(self) -> set[str]:
        return {date for date, _, _ in self.probed}


class FakeCache:
    def __init__(self, *, up: bool = True) -> None:
        self.up = up
        self.slots: dict[tuple[str, int], list[str]] = {}

    async def ping(self) -> bool:
        return self.up

    async def get_slots(self, date: str, size: int) -> list[str] | None:
        return self.slots.get((date, size))

    async def put_slots(self, date: str, size: int, free: list[str]) -> None:
        self.slots[(date, size)] = free


class FakeRuntime:
    def __init__(self, trace: list[str], *, api: Any = None, cache: Any = None) -> None:
        self.logger = FakeLogger()
        self.session = FakeSession(trace)
        self.api = api or FakeApi()
        self.cache = cache or FakeCache()
        self.agent = object()
        self.latency = _Summarisable()
        self.state = _State()
        self._trace = trace
        self.closed = False

    async def begin(self) -> None:
        self._trace.append("runtime.begin")

    async def finish(self) -> None:
        self._trace.append("runtime.finish")

    async def aclose(self) -> None:
        self.closed = True


class _Summarisable:
    def summary(self) -> dict[str, Any]:
        return {}


class _State:
    tool_calls: list[Any] = []
    created_reservations: list[dict[str, Any]] = []
    handoff: dict[str, Any] | None = None

    def redacted(self) -> dict[str, Any]:
        return {}


class FakeRoom:
    def __init__(self, name: str = "luma-test-room") -> None:
        self.name = name


class FakeProc:
    def __init__(self) -> None:
        self.userdata: dict[str, Any] = {}


class FakeJobContext:
    """Just enough JobContext to drive `entrypoint`."""

    def __init__(self, trace: list[str]) -> None:
        self.room = FakeRoom()
        self.proc = FakeProc()
        self.shutdown_callbacks: list[Any] = []
        self._trace = trace
        self.connected = False

    async def connect(self) -> None:
        self._trace.append("ctx.connect")
        self.connected = True

    def add_shutdown_callback(self, cb: Any) -> None:
        self.shutdown_callbacks.append(cb)


def _ok(data: Any) -> Any:
    from luma.api_client import ApiResult

    return ApiResult(ok=True, status=200, data=data)


def _fail() -> Any:
    from luma.api_client import ApiResult

    return ApiResult(ok=False, status=503, data=None, error_code="UPSTREAM")


@pytest.fixture
def trace() -> list[str]:
    return []


@pytest.fixture
def patched(monkeypatch, trace):
    """Replace `build_runtime` so no real STT/TTS/LLM client is constructed."""
    runtime = FakeRuntime(trace)
    monkeypatch.setattr(worker, "build_runtime", lambda *a, **k: runtime)
    return runtime


# ------------------------------------------------------- the regression test


@pytest.mark.asyncio
async def test_connects_to_the_room_before_starting_the_session(patched, trace):
    """The bug this file exists for.

    RoomIO only attaches handlers to an already-connected room, so starting the
    session first leaves the agent dispatched, blocked, and silent -- while
    every log line still looks healthy.
    """
    ctx = FakeJobContext(trace)

    await worker.entrypoint(ctx)

    assert "ctx.connect" in trace, "the worker never joined the room"
    assert "session.start" in trace
    assert trace.index("ctx.connect") < trace.index("session.start"), (
        f"session started before connecting to the room: {trace}"
    )


@pytest.mark.asyncio
async def test_greets_the_caller_after_the_session_starts(patched, trace):
    ctx = FakeJobContext(trace)

    await worker.entrypoint(ctx)

    assert trace.index("session.start") < trace.index("session.say")
    assert patched.session.said, "the caller was never greeted"


@pytest.mark.asyncio
async def test_opens_the_call_record_before_anything_else(patched, trace):
    """Turns and tool calls carry a foreign key to `calls`; if the parent row is
    not opened first they are rejected and silently swallowed."""
    ctx = FakeJobContext(trace)

    await worker.entrypoint(ctx)

    assert trace[0] == "runtime.begin", trace


@pytest.mark.asyncio
async def test_registers_a_shutdown_callback_that_closes_the_runtime(patched, trace):
    ctx = FakeJobContext(trace)

    await worker.entrypoint(ctx)

    assert len(ctx.shutdown_callbacks) == 1
    await ctx.shutdown_callbacks[0]()

    assert "runtime.finish" in trace
    assert patched.closed, "the runtime was never closed"
    assert "call_ended" in patched.logger.names()


# ----------------------------------------------------------------- prewarming


@pytest.mark.asyncio
async def test_prewarm_never_touches_the_date_the_api_fails_on():
    """The mock API returns its one and only 503 on the first availability
    request for `API_FAILURE_DATE`. Spending it on a background warm-up removes
    the transient-failure path from anything anyone could observe -- and this is
    live precisely when Redis is configured, which is the container setup."""
    api = FakeApi()
    runtime = FakeRuntime([], api=api, cache=FakeCache(up=True))

    await worker._prewarm(runtime)

    assert API_FAILURE_DATE in BOOKABLE_DATES, "the constant under test moved"
    assert API_FAILURE_DATE not in api.dates_probed(), (
        f"prewarm probed {API_FAILURE_DATE} and consumed the scripted 503"
    )
    assert api.dates_probed() == set(PREWARM_DATES)


@pytest.mark.asyncio
async def test_prewarm_fills_the_cache_for_the_common_party_sizes():
    api = FakeApi()
    cache = FakeCache(up=True)
    runtime = FakeRuntime([], api=api, cache=cache)

    await worker._prewarm(runtime)

    for date in PREWARM_DATES:
        for size in (2, 4):
            assert cache.slots[(date, size)] == list(SERVICE_SLOTS)
    assert "prewarm_complete" in runtime.logger.names()


@pytest.mark.asyncio
async def test_prewarm_stops_when_there_is_no_cache_to_fill():
    """Without Redis the prefetch has nowhere to put anything, so probing the
    API would be pure cost on the caller's behalf."""
    api = FakeApi()
    runtime = FakeRuntime([], api=api, cache=FakeCache(up=False))

    await worker._prewarm(runtime)

    assert api.health_calls == 1
    assert api.probed == [], "probed the API with no cache to store the result in"


@pytest.mark.asyncio
async def test_prewarm_skips_dates_already_cached():
    api = FakeApi()
    cache = FakeCache(up=True)
    for date in PREWARM_DATES:
        for size in (2, 4):
            cache.slots[(date, size)] = ["18:00"]
    runtime = FakeRuntime([], api=api, cache=cache)

    await worker._prewarm(runtime)

    assert api.probed == [], "re-probed slots that were already warm"


@pytest.mark.asyncio
async def test_prewarm_failure_is_swallowed():
    """Warming is best-effort. An exception escaping here would propagate out of
    the task created in `entrypoint` and surface as a failed call."""

    class Exploding(FakeApi):
        async def health(self) -> Any:
            raise RuntimeError("upstream is down")

    runtime = FakeRuntime([], api=Exploding())

    await worker._prewarm(runtime)  # must not raise

    assert "prewarm_failed" in runtime.logger.names()


@pytest.mark.asyncio
async def test_a_prewarm_failure_does_not_fail_the_call(monkeypatch, trace):
    """Belt and braces: even if `_prewarm` raised, the caller still gets a
    greeting, because it runs as a detached task."""

    async def explode(_: Any) -> None:
        raise RuntimeError("boom")

    runtime = FakeRuntime(trace)
    monkeypatch.setattr(worker, "build_runtime", lambda *a, **k: runtime)
    monkeypatch.setattr(worker, "_prewarm", explode)
    ctx = FakeJobContext(trace)

    await worker.entrypoint(ctx)
    await asyncio.sleep(0)  # let the detached task run and fail

    assert runtime.session.said, "a warm-up failure silenced the greeting"


# ------------------------------------------------------------ process wiring


def test_prewarm_hook_loads_the_vad_into_process_userdata(monkeypatch):
    """Loaded once per process. Doing it per call puts a few megabytes of ONNX
    directly into the caller's first-response latency."""
    sentinel = object()
    monkeypatch.setattr(worker.silero.VAD, "load", staticmethod(lambda *a, **k: sentinel))
    proc = FakeProc()

    worker.prewarm(proc)

    assert proc.userdata["vad"] is sentinel


def test_turn_detector_is_imported_in_the_main_process():
    """The Worker only spawns an inference executor if a runner was registered
    before it was constructed. Import the turn detector lazily inside the job
    process instead and every turn fails with "no inference executor",
    endpointing degrades to bare VAD, and the agent talks over people."""
    from livekit.agents.inference_runner import _InferenceRunner

    assert _InferenceRunner.registered_runners, (
        "no inference runner registered by importing luma.worker"
    )


def test_main_enables_multiprocess_metrics_before_the_worker_is_built(monkeypatch, tmp_path):
    """Calls run in job child processes. A counter incremented in a child lives
    in that child's memory and vanishes with it, so the parent would serve zeros
    forever -- unless the shared directory is configured before the fork."""
    order: list[str] = []
    target = tmp_path / "prom"

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(target))
    monkeypatch.setattr(
        worker.metrics, "enable_multiprocess", lambda d: order.append(f"enable:{d}")
    )
    monkeypatch.setattr(worker.cli, "run_app", lambda opts: order.append("run_app"))

    worker.main()

    assert order == [f"enable:{target}", "run_app"], order
