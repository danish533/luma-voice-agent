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
from .store import Cache, CallStore
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
    cache: Cache
    store: CallStore

    async def begin(self) -> None:
        """Open the call record. Must run before any turn or tool row.

        Lives here rather than in the worker so every entry point gets it --
        the evaluation harness drives the same agent through `build_runtime`,
        and when this was worker-only its tool-call writes all failed the
        foreign key and were swallowed by the background-write guard.
        """
        await self.store.create_schema()
        self.store.call_started(
            id=self.state.session_id,
            llm_model=self.settings.llm_model,
            stt_model=self.settings.stt_model,
            tts_model=self.settings.tts_model,
        )

    async def finish(self) -> None:
        """Close the call record out with its summary figures."""
        from datetime import datetime, timezone

        from .obs import last4

        summary = self.latency.summary()
        await self.store.call_ended(
            self.state.session_id,
            ended_at=datetime.now(timezone.utc),
            caller_name=self.state.caller_name,
            caller_phone_last4=last4(self.state.caller_phone),
            turn_count=summary.get("voice_turns") or 0,
            latency_p50_ms=summary.get("voice_p50_ms"),
            latency_p95_ms=summary.get("voice_p95_ms"),
            outcome=self.outcome(),
            handed_off=bool(self.state.handoff),
        )

    def outcome(self) -> str:
        """One word for what the call achieved, for the ops dashboard."""
        if self.state.created_reservations:
            return "booked"
        if self.state.handoff:
            return "handoff"
        for call in reversed(self.state.tool_calls):
            if call["status"] in {"modified", "cancelled"}:
                return call["status"]
        return "none"

    async def aclose(self) -> None:
        await self.api.aclose()
        await self.cache.aclose()
        await self.store.aclose()
        self.logger.close()


def _one_llm(provider: str, model: str, temperature: float) -> Any:
    if provider == "google":
        from livekit.plugins import google

        return google.LLM(model=model, temperature=temperature)
    if provider == "openai":
        from livekit.plugins import openai

        return openai.LLM(model=model, temperature=temperature)
    raise ValueError(f"Unsupported LLM provider {provider!r}; expected 'openai' or 'google'")


def build_llm(settings: Settings) -> Any:
    """The configured model, wrapped in a fallback if a second one is available.

    An LLM outage or a rate limit mid-call is otherwise dead air: the caller
    hears nothing and hangs up. With a fallback the turn is retried against the
    other provider and the caller notices a pause, not a failure. This is the
    one component with no graceful degradation of its own -- STT and TTS
    failures at least leave the conversation recoverable.

    `attempt_timeout` must be at least 10s. It is passed down as a request
    deadline, and Gemini rejects anything shorter outright -- "Manually set
    deadline 4s is too short. Minimum allowed deadline is 10s." A tighter value
    therefore breaks the fallback instead of speeding it up: the standby 400s
    before it can answer. The ceiling only bites when a provider *hangs*; a hard
    failure (401, 429, 5xx) switches immediately, which is the common case.
    """
    primary = _one_llm(settings.llm_provider, settings.llm_model, settings.llm_temperature)
    if not settings.llm_fallback_model:
        return primary

    from livekit.agents import llm as llm_mod

    secondary = _one_llm(
        settings.llm_fallback_provider, settings.llm_fallback_model, settings.llm_temperature
    )
    return llm_mod.FallbackAdapter([primary, secondary], attempt_timeout=10.0)


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
    cache = Cache(settings.redis_url)
    store = CallStore(settings.database_url)
    state = CallState(session_id=session_id)
    agent = LumaAgent(api=api, state=state, logger=logger, cache=cache, store=store)

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

    _wire_observability(session, logger, latency, state, store)
    return Runtime(
        settings=settings,
        logger=logger,
        latency=latency,
        api=api,
        state=state,
        agent=agent,
        session=session,
        cache=cache,
        store=store,
    )


def _wire_observability(
    session: AgentSession,
    logger: JsonLogger,
    latency: LatencyBook,
    state: CallState,
    store: CallStore,
) -> None:
    def _ms(report: Any, key: str) -> float | None:
        value = report.get(key)
        return round(value * 1000, 2) if isinstance(value, (int, float)) else None

    @session.on("user_input_transcribed")
    def _on_transcript(ev: Any) -> None:
        if ev.is_final:
            logger.log("user_transcript", text=ev.transcript)
            state.record_turn("caller", ev.transcript)
            store.record_turn(call_id=state.session_id, role="caller", text=ev.transcript)

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
        store.record_turn(
            call_id=state.session_id,
            role="agent",
            text=text,
            interrupted=interrupted,
            e2e_ms=turn.e2e_ms,
            eou_ms=turn.eou_delay_ms,
            llm_ttft_ms=turn.llm_ttft_ms,
            tts_ttfb_ms=turn.tts_ttfb_ms,
        )

    @session.on("agent_false_interruption")
    def _on_false_interruption(ev: Any) -> None:
        logger.log("false_interruption_recovered")

    @session.on("error")
    def _on_error(ev: Any) -> None:
        logger.log("session_error", source=str(getattr(ev, "source", "")), error=str(ev.error))
