"""Measure the two speech legs of the response-latency budget.

End-of-speech to first audio is the sum of three parts:

    STT finalisation  +  LLM time-to-first-token  +  TTS time-to-first-byte

The middle term is measured by the evaluation suite. This script measures the
outer two against the real providers, so the budget can be stated from
measurement rather than from vendor marketing. The true end-to-end figure comes
from a live call, where `obs.TurnLatency` joins all three on `speech_id`.

    python scripts/measure_speech_latency.py [--runs 5]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402
from livekit import rtc  # noqa: E402
from livekit.agents import stt as stt_mod  # noqa: E402
from livekit.agents.utils import http_context  # noqa: E402
from livekit.plugins import deepgram  # noqa: E402

from luma.config import Settings  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

SAMPLE_TEXT = (
    "Your table for four is confirmed for Friday, August fourteenth at six PM. "
    "Your confirmation code is LUMA four eight two one."
)
CALLER_TEXT = "I'd like a table for four on Friday August fourteenth at six PM."


async def measure_tts_ttfb(settings: Settings, runs: int) -> tuple[list[float], list[rtc.AudioFrame]]:
    """Time from request to the first audio frame, which is what a caller hears."""
    tts = deepgram.TTS(model=settings.tts_model)
    samples: list[float] = []
    frames: list[rtc.AudioFrame] = []
    for i in range(runs):
        started = time.perf_counter()
        first: float | None = None
        collected: list[rtc.AudioFrame] = []
        stream = tts.synthesize(CALLER_TEXT if i == 0 else SAMPLE_TEXT)
        async for event in stream:
            if first is None:
                first = (time.perf_counter() - started) * 1000
            collected.append(event.frame)
        await stream.aclose()
        if first is not None:
            samples.append(first)
        if i == 0:
            frames = collected  # reuse as synthetic caller audio for the STT leg
        await asyncio.sleep(0.3)
    await tts.aclose()
    return samples, frames


async def measure_stt_finalisation(
    settings: Settings, frames: list[rtc.AudioFrame], runs: int
) -> list[float]:
    """Time from the end of the caller's audio to the final transcript.

    Real speech is streamed in real time, so the frames are pushed at wall-clock
    pace; pushing them as fast as possible would measure nothing but bandwidth.
    """
    stt = deepgram.STT(
        model=settings.stt_model, language="en-US", interim_results=True, numerals=True
    )
    samples: list[float] = []
    for _ in range(runs):
        stream = stt.stream()
        transcript = ""

        async def pump() -> float:
            for frame in frames:
                stream.push_frame(frame)
                await asyncio.sleep(frame.samples_per_channel / frame.sample_rate)
            stream.end_input()
            return time.perf_counter()

        pump_task = asyncio.create_task(pump())
        audio_ended: float | None = None
        first_final: float | None = None
        try:
            async for event in stream:
                if event.type == stt_mod.SpeechEventType.FINAL_TRANSCRIPT:
                    if audio_ended is None and pump_task.done():
                        audio_ended = pump_task.result()
                    if audio_ended is not None and first_final is None:
                        first_final = time.perf_counter()
                        transcript = event.alternatives[0].text if event.alternatives else ""
                        break
        finally:
            await stream.aclose()
            if not pump_task.done():
                pump_task.cancel()

        if audio_ended and first_final:
            samples.append((first_final - audio_ended) * 1000)
            print(f"    heard: {transcript!r}")
        await asyncio.sleep(0.3)
    await stt.aclose()
    return samples


def report(label: str, samples: list[float]) -> float | None:
    if not samples:
        print(f"  {label:28} no samples")
        return None
    mean = statistics.mean(samples)
    print(
        f"  {label:28} mean {mean:6.0f} ms   min {min(samples):6.0f}   "
        f"max {max(samples):6.0f}   n={len(samples)}"
    )
    return mean


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    settings = Settings.from_env()
    print(f"STT: deepgram {settings.stt_model}    TTS: deepgram {settings.tts_model}\n")

    # Outside the agent worker the plugins have no shared aiohttp session, so
    # one has to be opened explicitly.
    async with http_context.open():
        print("Measuring TTS time-to-first-byte...")
        tts_samples, frames = await measure_tts_ttfb(settings, args.runs)
        audio_s = sum(f.samples_per_channel / f.sample_rate for f in frames)
        print(f"  (synthesised {audio_s:.1f}s of caller audio for the STT leg)")

        print("\nMeasuring STT finalisation after end of speech...")
        stt_samples = await measure_stt_finalisation(settings, frames, max(3, args.runs - 2))

    print("\nResults")
    tts_mean = report("TTS time-to-first-byte", tts_samples)
    stt_mean = report("STT finalisation", stt_samples)
    if tts_mean and stt_mean:
        print(
            f"\n  Speech legs total          {stt_mean + tts_mean:6.0f} ms "
            "(add LLM time-to-first-token for the full budget)"
        )


if __name__ == "__main__":
    asyncio.run(main())
