"""LiveKit worker entrypoint.

Run a browser call:      python -m luma.worker dev
Run in a terminal:       python -m luma.worker console
Production:              python -m luma.worker start
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from livekit.agents import JobContext, JobProcess, WorkerOptions, cli
from livekit.plugins import silero

# Imported here, at module scope in the *main* process, purely for the side
# effect of registering its inference runner. The Worker only spawns an
# inference executor if `_InferenceRunner.registered_runners` is non-empty at
# construction time. Importing the turn detector lazily inside the job process
# is too late: the model then fails on every single turn with "no inference
# executor" and endpointing silently degrades to bare VAD, which is what makes
# the agent talk over people mid-sentence.
from livekit.plugins.turn_detector import english as _turn_detector_en  # noqa: F401

from . import metrics
from .config import BOOKABLE_DATES, SERVICE_SLOTS, Settings
from .prompts import greeting
from .runtime import build_runtime

load_dotenv()
logger = logging.getLogger("luma")


def prewarm(proc: JobProcess) -> None:
    """Load the VAD once per process, not once per call.

    Silero is a few megabytes of ONNX; paying that on the first turn of a call
    would show up directly in the caller's first-response latency.
    """
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    settings = Settings.from_env()
    runtime = build_runtime(
        settings,
        session_id=ctx.room.name or None,
        voice=True,
        vad=ctx.proc.userdata.get("vad"),
    )

    await runtime.begin()
    runtime.logger.log(
        "call_started",
        room=ctx.room.name,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        stt_model=settings.stt_model,
        tts_model=settings.tts_model,
        api_base_url=settings.api_base_url,
    )

    async def _on_shutdown() -> None:
        await runtime.finish()
        runtime.logger.log(
            "call_ended",
            latency=runtime.latency.summary(),
            tool_calls=runtime.state.tool_calls,
            collected=runtime.state.redacted(),
            reservations=[r.get("confirmation_code") for r in runtime.state.created_reservations],
            handoff=(runtime.state.handoff or {}).get("handoff_id"),
        )
        await runtime.aclose()

    ctx.add_shutdown_callback(_on_shutdown)

    # Join the room before starting the session. RoomIO only attaches handlers
    # to an already-connected room -- its init task awaits the connected future
    # -- so without this the agent is dispatched, blocks forever, and the caller
    # hears nothing at all.
    await ctx.connect()
    runtime.logger.log("room_connected", room=ctx.room.name)

    # Warm the path while the caller is still hearing the greeting, so the
    # first tool call is not also paying for TCP, TLS and a cold cache.
    asyncio.create_task(_prewarm(runtime))

    await runtime.session.start(agent=runtime.agent, room=ctx.room)
    # A fixed greeting rather than a generated one: it is the one turn where
    # latency is fully avoidable, and it never needs to vary.
    await runtime.session.say(greeting(), allow_interruptions=True)


async def _prewarm(runtime) -> None:
    """Warm the connection, and the slot cache if Redis is configured.

    Only the *listing* is prefetched. `check_availability` is never served from
    cache: the write gate depends on it, and a booking authorised by a
    90-second-old snapshot is a booking made on a guess. Prefetching the common
    party sizes turns the usual opening question -- "what have you got on
    Saturday?" -- from six round trips into none.
    """
    try:
        await runtime.api.health()
        if not await runtime.cache.ping():
            return
        # Two and four cover most tables; anything else probes on demand.
        for date in BOOKABLE_DATES:
            for size in (2, 4):
                if await runtime.cache.get_slots(date, size) is not None:
                    continue
                results = await asyncio.gather(
                    *(runtime.api.check_availability(date, slot, size)
                      for slot in SERVICE_SLOTS)
                )
                free = [
                    slot for slot, r in zip(SERVICE_SLOTS, results)
                    if r.ok and r.data.get("available")
                ]
                await runtime.cache.put_slots(date, size, free)
        runtime.logger.log("prewarm_complete", dates=len(BOOKABLE_DATES))
    except Exception as exc:  # warming is best-effort and must never fail a call
        runtime.logger.log("prewarm_failed", error=str(exc))


def main() -> None:
    # Must happen before the worker forks any job process, or the children get
    # their own private counters and the parent reports zeros forever.
    multiproc_dir = os.getenv("PROMETHEUS_MULTIPROC_DIR", ".prometheus")
    metrics.enable_multiprocess(multiproc_dir)

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            # Served on :{port}/metrics alongside LiveKit's own worker gauges,
            # so Prometheus has one target per worker rather than two.
            prometheus_port=int(os.getenv("METRICS_PORT", "9091")),
            prometheus_multiproc_dir=multiproc_dir,
            # Keep a process warm. Without this the first call logs "no warmed
            # process available" and the caller waits several extra seconds in
            # silence before the greeting — long enough to say "hello? can you
            # hear me?" into a dead line.
            num_idle_processes=1,
        )
    )


if __name__ == "__main__":
    main()
