"""End-to-end smoke test for the voice path, without a microphone.

Joins a real LiveKit room as a caller, waits for the greeting, then *speaks* --
a sentence synthesised with Deepgram TTS and published as caller audio at
real-time pace. It then checks the agent's own logs to confirm the words were
transcribed, a tool was called, and the agent replied.

This is the check that would have caught the missing `ctx.connect()`: every
component registers fine and the caller simply never hears anything.

Requires `make api` and `make agent` to be running.

    python scripts/smoke_call.py                    # greeting + one spoken turn
    python scripts/smoke_call.py --greeting-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from livekit import api as lk_api  # noqa: E402
from livekit import rtc  # noqa: E402
from livekit.agents.utils import http_context  # noqa: E402
from livekit.plugins import deepgram  # noqa: E402

from luma.config import Settings  # noqa: E402

load_dotenv(ROOT / ".env")

CALLER_LINE = (
    "Hi, I would like to book a table for four people on Friday, "
    "August fourteenth, at six PM."
)


async def synthesise(text: str, model: str) -> list[rtc.AudioFrame]:
    """Turn a sentence into audio frames we can publish as if spoken."""
    tts = deepgram.TTS(model=model)
    frames: list[rtc.AudioFrame] = []
    stream = tts.synthesize(text)
    async for ev in stream:
        frames.append(ev.frame)
    await stream.aclose()
    await tts.aclose()
    return frames


def session_log(room_name: str) -> list[dict]:
    path = ROOT / Settings.from_env().log_dir / f"{room_name}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    url = os.environ["LIVEKIT_URL"]
    key, secret = os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
    room_name = f"luma-smoke-{int(time.time())}"

    token = (
        lk_api.AccessToken(key, secret)
        .with_identity("smoke-caller")
        .with_name("Smoke Caller")
        .with_grants(
            lk_api.VideoGrants(room_join=True, room=room_name, can_publish=True,
                               can_subscribe=True)
        )
        .to_jwt()
    )

    async with http_context.open():
        print("synthesising the caller's line…")
        speech = await synthesise(CALLER_LINE, settings.tts_model)
        rate = speech[0].sample_rate
        speech_s = sum(f.samples_per_channel / f.sample_rate for f in speech)
        print(f"  {speech_s:.1f}s of caller audio at {rate} Hz")

        room = rtc.Room()
        agent_joined = asyncio.Event()
        greeting_heard = asyncio.Event()
        reply_heard = asyncio.Event()
        listening = {"on": False}

        @room.on("participant_connected")
        def _on_join(p: rtc.RemoteParticipant) -> None:
            print(f"  agent joined: {p.identity}")
            agent_joined.set()

        @room.on("track_subscribed")
        def _on_track(track: rtc.Track, *_: object) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                asyncio.create_task(_drain(track))

        async def _drain(track: rtc.Track) -> None:
            """Audible frames, not merely a published track, are the proof."""
            stream = rtc.AudioStream(track)
            loud = 0
            async for ev in stream:
                if any(abs(s) > 500 for s in ev.frame.data[:400]):
                    loud += 1
                    if loud >= 3:
                        if not greeting_heard.is_set():
                            greeting_heard.set()
                        elif listening["on"]:
                            reply_heard.set()
                            break
                else:
                    loud = 0
            await stream.aclose()

        print(f"joining {room_name}")
        started = time.perf_counter()
        await room.connect(url, token)

        source = rtc.AudioSource(rate, 1)
        await room.local_participant.publish_track(
            rtc.LocalAudioTrack.create_audio_track("mic", source),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        print(f"  connected in {(time.perf_counter() - started) * 1000:.0f} ms")

        silent = rtc.AudioFrame.create(rate, 1, rate // 100)

        async def keep_silence() -> None:
            while True:
                await source.capture_frame(silent)
                await asyncio.sleep(0.01)

        hum = asyncio.create_task(keep_silence())
        try:
            await asyncio.wait_for(agent_joined.wait(), timeout=args.timeout)
            await asyncio.wait_for(greeting_heard.wait(), timeout=args.timeout)
            print(f"  greeting heard at {(time.perf_counter() - started) * 1000:.0f} ms")

            if args.greeting_only:
                print("\nPASS  greeting only (--greeting-only)")
                return 0

            # Let the greeting play out, or we would be barging in on it.
            await asyncio.sleep(args.settle)

            hum.cancel()
            print("  speaking the caller's line…")
            listening["on"] = True
            spoke_at = time.perf_counter()
            for frame in speech:
                await source.capture_frame(frame)
                await asyncio.sleep(frame.samples_per_channel / frame.sample_rate)
            hum = asyncio.create_task(keep_silence())

            await asyncio.wait_for(reply_heard.wait(), timeout=args.timeout)
            print(f"  agent replied {(time.perf_counter() - spoke_at - speech_s) * 1000:.0f} ms "
                  "after the caller stopped speaking")
            # Stay on the line while the reply plays out. TTS metrics are
            # emitted when the stream finishes, so hanging up the moment audio
            # is heard loses the TTS leg and the turn never assembles into a
            # latency figure.
            print(f"  staying on the line {args.linger:.0f}s so the turn completes…")
            await asyncio.sleep(args.linger)
        except asyncio.TimeoutError:
            print("\nFAIL  timed out.")
            print(f"      agent joined:  {agent_joined.is_set()}")
            print(f"      greeting:      {greeting_heard.is_set()}")
            print(f"      reply:         {reply_heard.is_set()}")
            if not agent_joined.is_set():
                print("      -> is `make agent` running?")
            elif not greeting_heard.is_set():
                print("      -> agent joined but stayed silent: check ctx.connect() and the TTS key.")
            return 1
        finally:
            hum.cancel()
            await asyncio.sleep(1.5)  # let the last log lines flush
            await room.disconnect()

    # The agent's own log is the ground truth for what it understood.
    rows = session_log(room_name)
    heard = [r["text"] for r in rows if r["event"] == "user_transcript"]
    tools = [r["tool"] for r in rows if r["event"] == "tool_call"]
    results = [(r["tool"], r["status"]) for r in rows if r["event"] == "tool_result"]

    print("\n--- what the agent understood ---")
    for t in heard:
        print(f"  transcript: {t!r}")
    for tool, status in results:
        print(f"  tool: {tool} -> {status}")

    ok = bool(heard) and "check_availability" in tools
    print(f"\n{'PASS' if ok else 'FAIL'}  "
          f"transcribed={bool(heard)}  called_check_availability={'check_availability' in tools}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--settle", type=float, default=4.0,
                        help="seconds to let the greeting finish before speaking")
    parser.add_argument("--linger", type=float, default=10.0,
                        help="seconds to stay connected after the reply starts")
    parser.add_argument("--greeting-only", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
