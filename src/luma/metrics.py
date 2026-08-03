"""Prometheus metrics for the things worth waking someone up about.

Registered on the default registry, which LiveKit already serves on
``:{prometheus_port}/metrics`` alongside its own worker gauges, so there is one
scrape target per worker rather than two.

**Multiprocess mode is not optional here.** Every call runs in a job child
process, so a counter incremented inside a tool lives in that child's memory
and vanishes when the call ends -- the parent's ``/metrics`` would report zero
tool calls forever while the agent worked perfectly. `PROMETHEUS_MULTIPROC_DIR`
makes the children write to shared files that the parent aggregates on scrape.

What is deliberately *not* here: anything with a caller's name, phone number or
transcript in a label. Prometheus labels are effectively permanent, unbounded
cardinality is what takes a metrics backend down, and a phone number in a label
is a phone number in every dashboard and alert forever.
"""

from __future__ import annotations

import os
from typing import Any

from prometheus_client import Counter, Histogram

# Buckets chosen for a phone call, not for a web request. The interesting
# region is 1-4 seconds: below one second feels instant, past four a caller
# assumes the line is dead. The default buckets bunch everything under 1s and
# tell you nothing about the range that matters.
_VOICE_BUCKETS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0)
_API_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

CALLS = Counter(
    "luma_calls_total",
    "Calls completed, by what they achieved.",
    ["outcome"],  # booked | modified | cancelled | handoff | none
)

TURN_LATENCY = Histogram(
    "luma_turn_latency_seconds",
    "End of the caller's speech to the first audio of the reply.",
    buckets=_VOICE_BUCKETS,
)

LEG_LATENCY = Histogram(
    "luma_turn_leg_seconds",
    "Per-component latency within a turn. Says which part to go and fix.",
    ["leg"],  # eou | llm_ttft | tts_ttfb
    buckets=_VOICE_BUCKETS,
)

# The most useful series in this file. A guardrail refusal is not an error, but
# a *change* in the refusal rate means a prompt edit has started pushing the
# model toward writes it should not be making -- which is exactly the failure
# that is invisible in logs until someone is double-booked.
TOOL_CALLS = Counter(
    "luma_tool_calls_total",
    "Tool invocations by outcome, including guardrail refusals.",
    ["tool", "status"],
)

DUPLICATES_PREVENTED = Counter(
    "luma_duplicates_prevented_total",
    "Duplicate bookings stopped, by which layer caught it.",
    ["layer"],  # in_call_memo | pre_write_search | redis_idempotency
)

HANDOFFS = Counter(
    "luma_handoffs_total",
    "Calls passed to a person.",
    ["reason"],
)

API_REQUESTS = Counter(
    "luma_reservation_api_requests_total",
    "Requests to the reservation API.",
    ["method", "path", "status"],
)

API_LATENCY = Histogram(
    "luma_reservation_api_seconds",
    "Reservation API round trip. Sits inside the caller's turn.",
    ["path"],
    buckets=_API_BUCKETS,
)

API_RETRIES = Counter(
    "luma_reservation_api_retries_total",
    "Requests that needed a retry, by whether the retry succeeded.",
    ["recovered"],
)

BARGE_INS = Counter("luma_barge_ins_total", "Turns the caller interrupted.")


def enable_multiprocess(directory: str) -> None:
    """Point prometheus_client at a shared directory before any child forks.

    Must run before the worker starts, and the directory must be empty: stale
    files from a previous run are counted as though they were live, so a
    restarted worker reports the sum of every run since the disk was last
    cleaned.
    """
    os.makedirs(directory, exist_ok=True)
    for stale in os.listdir(directory):
        if stale.endswith(".db"):
            os.remove(os.path.join(directory, stale))
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = directory


def record_turn(turn: Any, *, interrupted: bool) -> None:
    """One conversational turn. Interrupted turns are counted, not timed --
    their latency is truncated by definition and would flatter the histogram."""
    if interrupted:
        BARGE_INS.inc()
        return
    if turn.e2e_ms is not None:
        TURN_LATENCY.observe(turn.e2e_ms / 1000)
    for leg, value in (
        ("eou", turn.eou_delay_ms),
        ("llm_ttft", turn.llm_ttft_ms),
        ("tts_ttfb", turn.tts_ttfb_ms),
    ):
        if value is not None:
            LEG_LATENCY.labels(leg=leg).observe(value / 1000)


def record_api_call(*, method: str, path: str, status: int, ms: float, attempts: int) -> None:
    # The path is already a fixed set of endpoints, but ids are stripped anyway
    # so a future /reservations/{id} route cannot explode the cardinality.
    label = "/reservations/{id}" if path.startswith("/reservations/res_") else path
    API_REQUESTS.labels(method=method, path=label, status=str(status)).inc()
    API_LATENCY.labels(path=label).observe(ms / 1000)
    if attempts > 1:
        API_RETRIES.labels(recovered="true" if 200 <= status < 300 else "false").inc()
