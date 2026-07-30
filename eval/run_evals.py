"""Run the seven standard scenarios against the real agent and score them.

Text in, text out: the same prompt, tools, model and guardrails as the voice
worker, minus the microphone. That makes the suite deterministic enough to run
in CI and cheap enough to run on every prompt change, which a microphone-driven
suite never is.

Scoring never trusts what the agent *said*. Every scenario ends by asking the
reservation API what is actually on the books -- capacity consumed, records
written, statuses set. A model that narrates a perfect booking it never made
fails here.

    python eval/run_evals.py                 # all scenarios
    python eval/run_evals.py --only T2 T6    # a subset
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

from luma.api_client import ReservationApi  # noqa: E402
from luma.config import Settings  # noqa: E402
from luma.obs import LatencyBook  # noqa: E402
from luma.runtime import Runtime, build_runtime  # noqa: E402

load_dotenv()

RESULTS_DIR = Path(__file__).resolve().parent / "results"


# --------------------------------------------------------------------- model


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        return f"{'PASS' if self.passed else 'FAIL'}  {self.name}" + (
            f" -- {self.detail}" if self.detail else ""
        )


@dataclass
class ScenarioResult:
    id: str
    name: str
    checks: list[Check] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, str]] = field(default_factory=list)
    turn_latencies_ms: list[float] = field(default_factory=list)
    api_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)

    @property
    def duplicate_or_wrong_write(self) -> bool:
        """Any check tagged as a write-integrity check that did not hold."""
        return any(not c.passed for c in self.checks if c.name.startswith("write:"))

    def summary_row(self) -> dict[str, Any]:
        api_ms = [c["ms"] for c in self.api_calls]
        return {
            "test": self.id,
            "pass": self.passed,
            "tool_calls": len(self.tool_calls),
            "tool_call_names": [c["tool"] for c in self.tool_calls],
            "duplicate_or_wrong_write": self.duplicate_or_wrong_write,
            "turn_p50_ms": _pct(self.turn_latencies_ms, 50),
            "turn_p95_ms": _pct(self.turn_latencies_ms, 95),
            "api_p50_ms": _pct(api_ms, 50),
            "api_calls": len(api_ms),
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks],
            "error": self.error,
        }


def _pct(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered) + 0.5) - 1))
    return round(ordered[idx], 2)


# ------------------------------------------------------------------- helpers


class Ground:
    """Queries the API for what is actually on the books."""

    def __init__(self, api: ReservationApi) -> None:
        self._api = api

    async def by_phone(self, phone: str) -> list[dict[str, Any]]:
        result = await self._api.search_reservations(phone=phone)
        return result.data.get("results", []) if result.ok else []

    async def by_code(self, code: str) -> dict[str, Any] | None:
        result = await self._api.search_reservations(confirmation_code=code)
        records = result.data.get("results", []) if result.ok else []
        return records[0] if records else None

    async def remaining(self, date: str, time: str) -> int | None:
        result = await self._api.check_availability(date, time, 1)
        return result.data.get("remaining_capacity") if result.ok else None


def tool_names(runtime: Runtime) -> list[str]:
    return [c["tool"] for c in runtime.state.tool_calls]


def said(runtime: Runtime) -> str:
    return " ".join(t["text"] for t in runtime.state.transcript if t["role"] == "agent").lower()


def api_calls_to(runtime: Runtime, method: str, path_fragment: str) -> list[dict[str, Any]]:
    return [
        c
        for c in runtime.latency.api_calls
        if c["method"] == method and path_fragment in c["path"]
    ]


def confirmed(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("status") == "confirmed"]


# ----------------------------------------------------------------- scenarios


@dataclass
class Scenario:
    id: str
    name: str
    script: list[str]
    verify: Callable[[Runtime, Ground], Awaitable[list[Check]]]
    # Extra programmatic step after the conversation, for scenarios that
    # describe a repeated tool call rather than a spoken turn.
    epilogue: Callable[[Runtime], Awaitable[None]] | None = None


async def verify_t1(rt: Runtime, ground: Ground) -> list[Check]:
    records = await ground.by_phone("+13105550199")
    booked = confirmed(records)
    return [
        Check("checked availability", "check_availability" in tool_names(rt)),
        Check(
            "confirmed before writing",
            any(
                c["tool"] == "create_reservation" and c["arguments"].get("caller_confirmed")
                for c in rt.state.tool_calls
            ),
        ),
        Check(
            "write: exactly one reservation",
            len(booked) == 1,
            f"found {len(booked)}",
        ),
        Check(
            "write: correct details",
            bool(booked)
            and booked[0]["date"] == "2026-08-14"
            and booked[0]["time"] == "18:00"
            and booked[0]["party_size"] == 4,
            json.dumps(booked[0], default=str) if booked else "no reservation",
        ),
        Check(
            "write: only one create request",
            len(api_calls_to(rt, "POST", "/reservations")) == 1,
            f"{len(api_calls_to(rt, 'POST', '/reservations'))} POSTs",
        ),
    ]


async def verify_t2(rt: Runtime, ground: Ground) -> list[Check]:
    records = confirmed(await ground.by_phone("+14245550188"))
    spoken = said(rt)
    offered = any(token in spoken for token in ("7:30", "seven thirty", "seven-thirty"))
    return [
        Check("checked the requested 18:30 slot", "check_availability" in tool_names(rt)),
        Check("offered an API alternative aloud", offered, spoken[-300:]),
        Check(
            "did not invent availability at 18:30",
            not any(r["time"] == "18:30" for r in records),
        ),
        Check(
            "write: booked 19:30 for four",
            len(records) == 1
            and records[0]["time"] == "19:30"
            and records[0]["party_size"] == 4,
            json.dumps(records, default=str),
        ),
    ]


async def verify_t3(rt: Runtime, ground: Ground) -> list[Check]:
    records = confirmed(await ground.by_phone("+12135550114"))
    rechecked = [
        c
        for c in rt.state.tool_calls
        if c["tool"] == "check_availability" and c["arguments"].get("party_size") in (4, "4")
    ]
    return [
        Check("re-checked availability after the correction", bool(rechecked)),
        Check(
            "write: exactly one reservation",
            len(records) == 1,
            f"found {len(records)}",
        ),
        Check(
            "write: used the corrected party size of four",
            bool(records) and records[0]["party_size"] == 4,
            json.dumps(records, default=str),
        ),
        Check(
            "write: kept the original date and time",
            bool(records)
            and records[0]["date"] == "2026-08-15"
            and records[0]["time"] == "18:30",
        ),
    ]


async def verify_t4(rt: Runtime, ground: Ground) -> list[Check]:
    record = await ground.by_code("LUMA-4821")
    return [
        Check("searched before modifying", "find_reservation" in tool_names(rt)),
        Check(
            "confirmed before writing",
            any(
                c["tool"] == "modify_reservation" and c["arguments"].get("caller_confirmed")
                for c in rt.state.tool_calls
            ),
        ),
        Check(
            "write: moved to 19:30 for four",
            bool(record) and record["time"] == "19:30" and record["party_size"] == 4,
            json.dumps(record, default=str),
        ),
        Check(
            "write: patched the existing record, did not create a new one",
            bool(record) and record["reservation_id"] == "res_existing_4821"
            and not api_calls_to(rt, "POST", "/reservations"),
        ),
        Check(
            "write: original 18:00 seats released",
            await ground.remaining("2026-08-14", "18:00") == 6,
        ),
    ]


async def verify_t5(rt: Runtime, ground: Ground) -> list[Check]:
    record = await ground.by_code("LUMA-4821")
    cancels = api_calls_to(rt, "POST", "/cancel")
    return [
        Check("searched before cancelling", "find_reservation" in tool_names(rt)),
        Check(
            "asked for explicit confirmation",
            any(
                c["tool"] == "cancel_reservation" and c["arguments"].get("caller_confirmed")
                for c in rt.state.tool_calls
            ),
        ),
        Check("write: reservation is cancelled", bool(record) and record["status"] == "cancelled"),
        Check("write: cancelled exactly once", len(cancels) == 1, f"{len(cancels)} cancel calls"),
        Check(
            "write: seats returned to the pool",
            await ground.remaining("2026-08-14", "18:00") == 6,
        ),
    ]


async def verify_t6(rt: Runtime, ground: Ground) -> list[Check]:
    availability = api_calls_to(rt, "GET", "/availability")
    statuses = [c["status"] for c in availability]
    spoken = said(rt)
    return [
        Check("first availability request received a 503", 503 in statuses, str(statuses)),
        Check("recovered on retry", 200 in statuses, str(statuses)),
        Check(
            "retried at most once per request",
            all(c["attempts"] <= 2 for c in availability),
            str([c["attempts"] for c in availability]),
        ),
        Check(
            "no retry storm",
            len(availability) <= 3,
            f"{len(availability)} availability requests",
        ),
        Check(
            "did not claim a result it never received",
            "unable" not in spoken or "available" in spoken,
            spoken[-300:],
        ),
        Check(
            "write: nothing was booked on a check-only request",
            not api_calls_to(rt, "POST", "/reservations"),
        ),
    ]


async def repeat_create(rt: Runtime) -> None:
    """T7 asks for the create call to be repeated with the same idempotency key.

    Replaying the model's own last create call is the faithful reproduction:
    identical arguments, identical derived key.
    """
    last = next(
        (c for c in reversed(rt.state.tool_calls) if c["tool"] == "create_reservation"),
        None,
    )
    if not last:
        return
    args = dict(last["arguments"])
    await rt.agent.create_reservation(
        None,
        name=args.get("name") or "Morgan Reed",
        phone=rt.state.caller_phone or "+13105550166",
        date=args["date"],
        time=args["time"],
        party_size=args["party_size"],
        caller_confirmed=True,
    )


async def verify_t7(rt: Runtime, ground: Ground) -> list[Check]:
    records = confirmed(await ground.by_phone("+13105550166"))
    creates = api_calls_to(rt, "POST", "/reservations")
    remaining = await ground.remaining("2026-08-14", "20:00")
    return [
        Check(
            "write: exactly one reservation record",
            len(records) == 1,
            f"found {len(records)}",
        ),
        Check(
            "write: capacity consumed once",
            remaining == 4,
            f"remaining {remaining}, expected 4 (6 minus a party of 2)",
        ),
        Check(
            "repeated create returned the same reservation",
            any(
                c["tool"] == "create_reservation"
                and c["status"] in {"already_created", "duplicate_reservation_exists"}
                for c in rt.state.tool_calls
            ),
            str([c["status"] for c in rt.state.tool_calls if c["tool"] == "create_reservation"]),
        ),
        Check(
            "at most one create reached the API",
            len(creates) <= 1,
            f"{len(creates)} POSTs to /reservations",
        ),
    ]


SCENARIOS: list[Scenario] = [
    Scenario(
        "T1",
        "Create available reservation",
        [
            "Reserve a table for four on Friday, August 14 at 6 PM.",
            "Jordan Lee, 310-555-0199.",
            "No notes.",
            "Yes, confirm.",
        ],
        verify_t1,
    ),
    Scenario(
        "T2",
        "Unavailable time",
        [
            "Book four people Friday, August 14 at 6:30 PM.",
            "I can do 7:30 PM instead.",
            "Taylor Kim, 424-555-0188.",
            "Confirm.",
        ],
        verify_t2,
    ),
    Scenario(
        "T3",
        "Correction and barge-in",
        [
            "Saturday, August 15 at 6:30 PM for two.",
            "Casey Brown, 213-555-0114.",
            "Sorry, make that four people.",
            "Confirm.",
        ],
        verify_t3,
    ),
    Scenario(
        "T4",
        "Modify existing reservation",
        [
            "Change reservation LUMA-4821.",
            "Move it to 7:30 PM on the same date and make it four people.",
            "Confirm.",
        ],
        verify_t4,
    ),
    Scenario(
        "T5",
        "Cancel existing reservation",
        ["Cancel reservation LUMA-4821.", "Yes, cancel it."],
        verify_t5,
    ),
    Scenario(
        "T6",
        "Temporary API failure",
        [
            "Check Sunday, August 16 at 6 PM for two.",
            "If it is temporarily unavailable, try once more. If it still fails, hand me off.",
        ],
        verify_t6,
    ),
    Scenario(
        "T7",
        "Duplicate protection",
        [
            "Book Friday, August 14 at 8 PM for two.",
            "Morgan Reed, 310-555-0166.",
            "Confirm.",
        ],
        verify_t7,
        epilogue=repeat_create,
    ),
]


# -------------------------------------------------------------------- runner


async def run_scenario(scenario: Scenario, settings: Settings) -> ScenarioResult:
    result = ScenarioResult(id=scenario.id, name=scenario.name)
    runtime = build_runtime(settings, session_id=f"eval_{scenario.id}", voice=False)

    # Every scenario declares reset_before_test, so start from the fixed seed.
    reset = await runtime.api.reset()
    if not reset.ok:
        result.error = f"could not reset API at {settings.api_base_url}"
        await runtime.aclose()
        return result

    try:
        await runtime.begin()
        await runtime.session.start(agent=runtime.agent)
        for turn in scenario.script:
            runtime.state.record_turn("caller", turn)
            started = time.perf_counter()
            await runtime.session.run(user_input=turn)
            result.turn_latencies_ms.append(round((time.perf_counter() - started) * 1000, 2))

        if scenario.epilogue:
            await scenario.epilogue(runtime)

        # Ground truth is read on a separate client so its calls do not pollute
        # the scenario's own API-call accounting.
        ground_api = ReservationApi(settings, runtime.logger, LatencyBook())
        try:
            result.api_calls = list(runtime.latency.api_calls)
            result.checks = await scenario.verify(runtime, Ground(ground_api))
        finally:
            await ground_api.aclose()

        result.tool_calls = list(runtime.state.tool_calls)
        result.transcript = list(runtime.state.transcript)
        await runtime.finish()
    except Exception as exc:  # a crashed scenario is a failed scenario
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await runtime.session.aclose()
        except Exception:
            pass
        await runtime.aclose()

    return result


def render_markdown(results: list[ScenarioResult], settings: Settings) -> str:
    rows = [
        "| Test | Pass/Fail | Final outcome | Tool calls | Duplicate/wrong write? |"
        " End-of-speech to first audio | API latency (p50) | Notes |",
        "|---|---|---|---|---|---:|---:|---|",
    ]
    for r in results:
        row = r.summary_row()
        failed = [c.name for c in r.checks if not c.passed]
        outcome = r.error or ("all checks passed" if r.passed else f"failed: {', '.join(failed)}")
        rows.append(
            f"| {r.id} | {'PASS' if r.passed else 'FAIL'} | {outcome} | "
            f"{row['tool_calls']} ({', '.join(row['tool_call_names'])}) | "
            f"{'YES' if r.duplicate_or_wrong_write else 'no'} | "
            f"see voice run | {row['api_p50_ms']} ms | {r.name} |"
        )

    passed = sum(1 for r in results if r.passed)
    all_turns = [t for r in results for t in r.turn_latencies_ms]
    all_api = [c["ms"] for r in results for c in r.api_calls]
    total_checks = sum(len(r.checks) for r in results)
    passed_checks = sum(1 for r in results for c in r.checks if c.passed)

    rows += [
        "",
        f"- Model: `{settings.llm_provider}:{settings.llm_model}`",
        f"- Task success rate: **{passed}/{len(results)}**"
        f" ({passed / len(results) * 100:.0f}%)",
        f"- Check-level pass rate: **{passed_checks}/{total_checks}**",
        f"- Duplicate/wrong writes: **{sum(1 for r in results if r.duplicate_or_wrong_write)}**",
        f"- Text-mode turn latency (LLM + tools, no audio): p50 {_pct(all_turns, 50)} ms,"
        f" p95 {_pct(all_turns, 95)} ms",
        f"- Reservation API latency: p50 {_pct(all_api, 50)} ms, p95 {_pct(all_api, 95)} ms",
    ]
    return "\n".join(rows)


def looks_like_quota_exhaustion(error: str | None) -> bool:
    """Free LLM tiers meter by requests-per-minute, and one scenario is a dozen
    requests. The symptom is a connection error after the provider SDK has
    already burned its own retries inside the same minute."""
    if not error:
        return False
    markers = ("APIConnectionError", "RESOURCE_EXHAUSTED", "429", "503", "UNAVAILABLE")
    return any(m in error for m in markers)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", help="scenario ids, e.g. T2 T6")
    parser.add_argument("--json", default=None, help="path for the raw results JSON")
    parser.add_argument(
        "--pace",
        type=float,
        default=float(os.getenv("EVAL_PACE_S", "20")),
        help="seconds to wait between scenarios, to stay inside free-tier rate limits",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=float(os.getenv("EVAL_COOLDOWN_S", "60")),
        help="seconds to wait before retrying a scenario that hit a rate limit",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    chosen = [s for s in SCENARIOS if not args.only or s.id in args.only]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results: list[ScenarioResult] = []

    for index, scenario in enumerate(chosen):
        if index and args.pace:
            await asyncio.sleep(args.pace)

        print(f"\n=== {scenario.id}  {scenario.name} ===", flush=True)
        result = await run_scenario(scenario, settings)

        # A rate limit is an artefact of the harness, not a failure of the
        # agent. Wait out the window and give the scenario one clean run.
        if looks_like_quota_exhaustion(result.error) and args.cooldown:
            print(f"  rate limited; cooling down {args.cooldown:.0f}s and retrying", flush=True)
            await asyncio.sleep(args.cooldown)
            result = await run_scenario(scenario, settings)

        results.append(result)
        if result.error:
            print(f"  ERROR: {result.error}")
        for check in result.checks:
            print(f"  {check}")

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "llm": f"{settings.llm_provider}:{settings.llm_model}",
        "api_base_url": settings.api_base_url,
        "scenarios": [r.summary_row() for r in results],
        "transcripts": {r.id: r.transcript for r in results},
    }
    json_path = Path(args.json) if args.json else RESULTS_DIR / "results.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    markdown = render_markdown(results, settings)
    (RESULTS_DIR / "results.md").write_text(markdown + "\n")

    print("\n" + markdown)
    print(f"\nWrote {json_path} and {RESULTS_DIR / 'results.md'}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
