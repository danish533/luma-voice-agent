"""Render the greeting in several Deepgram Aura-2 voices so you can pick one.

Voice choice is a judgement call that can only be made by listening. This writes
one WAV per candidate into `voice_samples/`; play them and set
DEEPGRAM_TTS_MODEL in .env to the one you like.

    python scripts/voice_samples.py
    # then: ffplay -autoexit -nodisp voice_samples/aura-2-<name>-en.wav
"""

from __future__ import annotations

import asyncio
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from livekit.agents.utils import http_context  # noqa: E402
from livekit.plugins import deepgram  # noqa: E402

from luma.prompts import greeting  # noqa: E402

load_dotenv(ROOT / ".env")

# Aura-2 English voices worth auditioning for a warm, conversational host.
CANDIDATES = [
    "aura-2-thalia-en",     # current default
    "aura-2-luna-en",
    "aura-2-asteria-en",
    "aura-2-athena-en",
    "aura-2-hera-en",
    "aura-2-cora-en",
    "aura-2-andromeda-en",
]

OUT = ROOT / "voice_samples"


async def render(model: str) -> str:
    tts = deepgram.TTS(model=model)
    frames = []
    rate = 24000
    try:
        stream = tts.synthesize(greeting())
        async for ev in stream:
            frames.append(bytes(ev.frame.data))
            rate = ev.frame.sample_rate
        await stream.aclose()
    except Exception as exc:  # an unavailable voice should not stop the rest
        return f"  {model:24} unavailable ({type(exc).__name__})"
    finally:
        await tts.aclose()

    path = OUT / f"{model}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(frames))
    seconds = sum(len(f) for f in frames) / 2 / rate
    return f"  {model:24} {seconds:4.1f}s  ->  {path.relative_to(ROOT)}"


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    print(f'Rendering: "{greeting()}"\n')
    async with http_context.open():
        for model in CANDIDATES:
            print(await render(model), flush=True)
    print(
        "\nPlay them, then set DEEPGRAM_TTS_MODEL in .env to your pick.\n"
        "  ffplay -autoexit -nodisp voice_samples/aura-2-luna-en.wav"
    )


if __name__ == "__main__":
    asyncio.run(main())
