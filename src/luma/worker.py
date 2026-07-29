"""LiveKit worker entrypoint.

Run a browser call:      python -m luma.worker dev
Run in a terminal:       python -m luma.worker console
Production:              python -m luma.worker start
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv
from livekit.agents import JobContext, JobProcess, WorkerOptions, cli
from livekit.plugins import silero

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

    await runtime.session.start(agent=runtime.agent, room=ctx.room)
    # A fixed greeting rather than a generated one: it is the one turn where
    # latency is fully avoidable, and it never needs to vary.
    await runtime.session.say(GREETING, allow_interruptions=True)


def main() -> None:
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))


if __name__ == "__main__":
    main()
