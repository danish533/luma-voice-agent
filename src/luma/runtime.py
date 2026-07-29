"""Builds the agent session.

Shared by the voice worker and the evaluation harness so the thing under test is
the thing that ships: same prompt, same tools, same model, same temperature.
Only the transport differs -- microphone and speaker versus text in, text out.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from livekit.agents import AgentSession

from .agent import LumaAgent
from .api_client import ReservationApi
from .config import Settings
from .obs import JsonLogger, LatencyBook, TurnLatency
from .state import CallState


@dataclass
class Runtime:
    settings: Settings
    logger: JsonLogger
    latency: LatencyBook
    api: ReservationApi
    state: CallState
    agent: LumaAgent
    session: AgentSession

    async def aclose(self) -> None:
        await self.api.aclose()
        self.logger.close()


def build_llm(settings: Settings) -> Any:
    """Model choice is env-driven so provider swaps need no code change."""
    if settings.llm_provider == "google":
        from livekit.plugins import google

        return google.LLM(model=settings.llm_model, temperature=settings.llm_temperature)
    if settings.llm_provider == "openai":
        from livekit.plugins import openai

        return openai.LLM(model=settings.llm_model, temperature=settings.llm_temperature)
    raise ValueError(
        f"Unsupported LLM_PROVIDER {settings.llm_provider!r}; expected 'openai' or 'google'"
    )


def build_turn_detector(settings: Settings) -> Any:
    """Semantic end-of-turn detection, local or hosted.

    Deciding whether the caller has actually finished speaking sits on the most
    latency-sensitive path in the call, so the default runs the model in
    process: no network round trip, and `console` mode works without any
    LiveKit credentials. The hosted variant moves ~4-8 sessions/vCPU of ONNX
    work off the worker, which is the better trade once concurrency is the
    binding constraint (see ARCHITECTURE.md Q9).
    """
    if settings.turn_detector == "cloud":
        from livekit.agents.inference import TurnDetector

        return TurnDetector()
    if settings.turn_detector == "local":
        # Deprecated upstream in favour of the hosted model above, but retained
        # deliberately: it is the lower-latency option at this scale.
        from livekit.plugins.turn_detector.english import EnglishModel

        return EnglishModel()
    raise ValueError(
        f"Unsupported TURN_DETECTOR {settings.turn_detector!r}; expected 'local' or 'cloud'"
    )


def build_runtime(
    settings: Settings,
    *,
    session_id: str | None = None,
    voice: bool = True,
    vad: Any = None,
) -> Runtime:
    session_id = session_id or f"call_{uuid.uuid4().hex[:10]}"
    logger = JsonLogger(session_id, log_dir=settings.log_dir, level=settings.log_level)
    latency = LatencyBook()
    api = ReservationApi(settings, logger, latency)
    state = CallState(session_id=session_id)
    agent = LumaAgent(api=api, state=state, logger=logger)

    if voice:
        from livekit.plugins import deepgram

        session = AgentSession(
            stt=deepgram.STT(
                model=settings.stt_model,
                language="en-US",
                # Interim results are what make barge-in feel instant: the
                # pipeline reacts to partial speech instead of waiting for a
                # final transcript.
                interim_results=True,
                punctuate=True,
                # Emit "310" rather than "three one zero" -- phone numbers and
                # party sizes then survive normalisation intact.
                numerals=True,
                # Bias the acoustic model toward vocabulary this agent hears.
                keyterm=["Luma", "Luma Bistro", "reservation", "confirmation code"],
            ),
            llm=build_llm(settings),
            tts=deepgram.TTS(model=settings.tts_model),
            vad=vad,
            # One declarative block rather than the deprecated per-kwarg form.
            # That matters beyond tidiness: when a turn_handling dict is passed,
            # the loose kwargs are ignored, so mixing the two silently drops
            # whatever you set.
            turn_handling={
                # A transformer that decides whether the caller has actually
                # finished, rather than trusting a fixed silence timeout. This
                # is what stops the agent talking over someone mid-thought.
                "turn_detection": build_turn_detector(settings),
                "endpointing": {
                    "mode": "fixed",
                    "min_delay": 0.35,
                    # The single biggest latency win available. The default
                    # 4.0s ceiling applies whenever the detector is unsure --
                    # and on a hesitant caller ("yeah... I would like...") it is
                    # unsure often, so four seconds of silence lands mid-booking
                    # and reads as a dead line. 1.2s still protects a genuine
                    # pause without stranding anyone.
                    "max_delay": 1.2,
                },
                "interruption": {
                    "enabled": True,
                    # Two guards against a cough or an "mhm" cutting the agent
                    # off: speech must last long enough and carry enough words.
                    "min_duration": 0.5,
                    "min_words": 2,
                    "discard_audio_if_uninterruptible": True,
                    # If the interruption turns out to have been noise, resume
                    # rather than leaving the reply half-spoken.
                    "resume_false_interruption": True,
                    "false_interruption_timeout": 2.0,
                },
                # Safe here because the prompt and tool set are fixed for the
                # whole call: a draft started early is never invalidated. An
                # agent that rebuilt either per turn would have to disable this.
                "preemptive_generation": {"enabled": True},
            },
        )
    else:
        session = AgentSession(llm=build_llm(settings))

    _wire_observability(session, logger, latency, state)
    return Runtime(
        settings=settings,
        logger=logger,
        latency=latency,
        api=api,
        state=state,
        agent=agent,
        session=session,
    )


def _wire_observability(
    session: AgentSession, logger: JsonLogger, latency: LatencyBook, state: CallState
) -> None:
    def _ms(report: Any, key: str) -> float | None:
        value = report.get(key)
        return round(value * 1000, 2) if isinstance(value, (int, float)) else None

    @session.on("user_input_transcribed")
    def _on_transcript(ev: Any) -> None:
        if ev.is_final:
            logger.log("user_transcript", text=ev.transcript)
            state.record_turn("caller", ev.transcript)

    # `end_of_turn_delay` is reported on the *user* message; the LLM/TTS legs
    # and the end-to-end figure on the *assistant* reply that follows it. They
    # have to be stitched across the two.
    pending_eou: dict[str, float | None] = {"ms": None}

    @session.on("conversation_item_added")
    def _on_item(ev: Any) -> None:
        item = ev.item
        role = getattr(item, "role", None)

        if role == "user":
            pending_eou["ms"] = _ms(getattr(item, "metrics", None) or {}, "end_of_turn_delay")
            return
        if role != "assistant":
            return

        text = item.text_content or ""
        interrupted = bool(getattr(item, "interrupted", False))
        state.record_turn("agent", text)
        # `interrupted` is set when the caller barged in mid-sentence, and
        # `text` is what the caller actually heard before being cut off --
        # not what the model intended to say.
        logger.log("agent_turn", interrupted=interrupted, chars=len(text), text=text)

        report = getattr(item, "metrics", None) or {}
        turn = TurnLatency(
            e2e_ms=_ms(report, "e2e_latency"),
            eou_delay_ms=pending_eou["ms"],
            llm_ttft_ms=_ms(report, "llm_node_ttft"),
            tts_ttfb_ms=_ms(report, "tts_node_ttfb"),
        )
        pending_eou["ms"] = None
        if turn.e2e_ms is None:
            return  # the fixed greeting has no preceding user turn to measure
        # A barge-in truncates the turn, so its timings would drag the
        # percentiles toward numbers no caller experienced.
        if not interrupted:
            latency.record_turn(turn)
        logger.log("turn_latency", interrupted=interrupted, **turn.as_dict())

    @session.on("agent_false_interruption")
    def _on_false_interruption(ev: Any) -> None:
        logger.log("false_interruption_recovered")

    @session.on("error")
    def _on_error(ev: Any) -> None:
        logger.log("session_error", source=str(getattr(ev, "source", "")), error=str(ev.error))
