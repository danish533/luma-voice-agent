"""LiveKit worker entrypoint.

Run a browser call:      python -m luma.worker dev
Run in a terminal:       python -m luma.worker console
Production:              python -m luma.worker start
"""

from __future__ import annotations

import asyncio
import logging

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

from .config import Settings
from .prompts import GREETING
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

    # Warm the reservation API's connection while the caller is still hearing
    # the greeting: TCP and TLS setup then lands off the critical path instead
    # of inside the first tool call. Deliberately a health check and not an
    # availability sweep -- caching which tables are free would mean answering
    # from a snapshot, and this agent's central rule is that availability comes
    # fresh from the API every time.
    warm = asyncio.create_task(runtime.api.health())
    warm.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    await runtime.session.start(agent=runtime.agent, room=ctx.room)
    # A fixed greeting rather than a generated one: it is the one turn where
    # latency is fully avoidable, and it never needs to vary.
    await runtime.session.say(GREETING, allow_interruptions=True)


def main() -> None:
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            # Keep a process warm. Without this the first call logs "no warmed
            # process available" and the caller waits several extra seconds in
            # silence before the greeting — long enough to say "hello? can you
            # hear me?" into a dead line.
            num_idle_processes=1,
        )
    )


if __name__ == "__main__":
    main()
