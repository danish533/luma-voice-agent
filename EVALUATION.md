# Evaluation Results

Model: **`google:gemini-3.1-flash-lite`** · STT **Deepgram Nova-3** · TTS
**Deepgram Aura-2** · run 2026-07-29.

Reproduce with `make api` then `make eval`. Raw output, including full
transcripts, is in `eval/results/results.json`.

Every scenario is preceded by `POST /admin/reset`, as
`standard_test_cases.json` requires. **Scoring never trusts what the agent
said** — each scenario ends by asking the API what is actually on the books:
records written, capacity consumed, statuses set. An agent that narrates a
booking it never made fails here.

---

## Standard test scenarios

| Test | Pass/Fail | Final outcome | Tool calls | Duplicate/wrong write? | End-of-speech to first audio | API latency (p50) | Notes |
|---|---|---|---|---|---:|---:|---|
| T1 | **PASS** | One reservation, Jordan Lee, 2026-08-14 18:00, party 4 | 2 — `check_availability` → `create_reservation` | no | ~1.4 s (budget) | 8.2 ms | Exactly one `POST /reservations` |
| T2 | **PASS** | Booked 19:30 for 4 after 18:30 came back full | 3 — `check_availability` ×2 → `create_reservation` | no | ~1.4 s | 8.6 ms | Offered the API's own alternatives; nothing booked at 18:30 |
| T3 | **PASS** | One reservation, 2026-08-15 18:30, **party 4** | 3 — `check_availability` ×2 → `create_reservation` | no | ~1.4 s | 9.3 ms | Correction forced a fresh availability check before the write |
| T4 | **PASS** | `res_existing_4821` moved to 19:30, party 4 | 3 — `find_reservation` → `check_availability` → `modify_reservation` | no | ~1.4 s | 8.9 ms | Patched in place; 18:00 seats released |
| T5 | **PASS** | `res_existing_4821` cancelled | 2 — `find_reservation` → `cancel_reservation` | no | ~1.4 s | 8.9 ms | One cancel call; seats returned to the pool |
| T6 | **PASS** | 503 then success; result reported honestly | 1 — `check_availability` | no | ~1.4 s | 8.1 ms | Statuses `[503, 200]`, 2 attempts, no retry storm |
| T7 | **PASS** | One reservation, capacity consumed once | 3 — `check_availability` → `create_reservation` ×2 | no | ~1.4 s | 7.7 ms | Repeat create returned `already_created` without a second POST |

### Aggregate

| Metric | Result |
|---|---|
| **Task success rate** | **7 / 7 (100%)** |
| **Check-level pass rate** | **33 / 33 (100%)** |
| **Tool-call accuracy** | 17 / 17 calls correct — right tool, right arguments, right order |
| **Duplicate-write rate** | **0 / 7** |
| **Wrong-write rate** | **0 / 7** |
| Reservation API latency | p50 **8.9 ms**, p95 **12.3 ms** |
| Text-mode turn latency (LLM + tools, no audio) | p50 **1,617 ms**, p95 **2,377 ms** |

Note on tool-call counts: T2 and T3 legitimately call `check_availability`
twice — once for the time the caller first asked for, once after they changed
it. T4's `check_availability` before `modify_reservation` is not required by the
guardrails but is correct behaviour. T7's second `create_reservation` is the
scenario's own "repeat the create call" step, and it was absorbed by the in-call
duplicate memo without reaching the API.

---

## Latency

Response latency is measured per component and joined per turn on LiveKit's
shared `speech_id` (`obs.TurnLatency`), so the reported figure is what the
caller actually experiences rather than a single vendor's number.

### Measured components

`scripts/measure_speech_latency.py`, against the live providers:

| Leg | Mean | Min | Max | n |
|---|---:|---:|---:|---:|
| Deepgram Nova-3 — end of speech to final transcript | **266 ms** | 244 | 288 | 4 |
| `gemini-3.1-flash-lite` — time to first token | **723 ms** | 555 | 1,456 | 6 |
| Deepgram Aura-2 — time to first audio byte | **414 ms** | 302 | 890 | 6 |

### Budget

```
end-of-speech ─► STT finalisation   266 ms
              ─► endpointing + turn detector  ~400 ms  (min_endpointing_delay)
              ─► LLM time-to-first-token      723 ms
              ─► TTS time-to-first-byte       414 ms
                                            ─────────
                 first audio                ~1.4–1.8 s
```

This is a component budget, and it is labelled as such in the table above. The
end-to-end figure is emitted per turn as a `turn_latency` log line during any
live call, and appears in the demo recording. Two caveats worth stating: the
LiveKit worker registered in **India West**, so a deployment co-located with the
caller would shave a round trip off every leg; and `preemptive_generation` hides
part of the LLM leg when the turn detector's guess is right, which the component
budget above does not credit.

The reservation API itself is not a factor — p50 **8.9 ms** against a local mock.
In production this would be the one number to watch, since it sits inside the
turn.

---

## Deterministic guardrail suite

Separate from the scenarios above, `make test` runs **70 tests** covering
normalisation and the write guardrails directly against the mock API, with no
model in the loop. They are fast (~3 s), deterministic, and need no LLM key,
which makes them CI-appropriate in a way that a model-driven suite is not.

```
70 passed in 3.00s
```

They assert the properties that must hold regardless of what the model does:
every phone spelling normalises to `+13105550147`; no create without a verified
availability check; no write without confirmation; a corrected party size
invalidates the earlier check; a repeated booking consumes capacity exactly
once; unknown reservation ids are refused; the 503 is retried exactly once.

---

## Known issues found during evaluation

**Free-tier rate limiting, not an agent failure.** The first full run passed T1
and T2 then failed T3–T7 with `APIConnectionError`. Each scenario is ~8–12 LLM
requests, which exhausts the free Gemini per-minute quota. Confirmed by running
T3 alone — it passed 4/4. The harness now paces scenarios (`--pace`, default
20 s) and retries once after a cooldown when it detects quota exhaustion. The
figures above are from a clean paced run.

**Model selection was a latency decision.** `gemini-3.6-flash` and
`gemini-flash-latest` both call tools correctly but average **6–12 s** per
completion because of default extended thinking, and `3.6-flash` rejects
`thinking_budget=0` outright. `gemini-2.5-flash` is retired for new API keys.
`gemini-3.1-flash-lite` was chosen on measurement: ~650 ms completions, correct
tool calls, and it is the closest available model to the requested "3.1 flash"
(there is no non-lite `gemini-3.1-flash` text model).

**Barge-in is not covered by the automated suite.** The harness is text-mode, so
T3 verifies *correction semantics* — that changing the party size invalidates
the earlier availability check and yields exactly one reservation at the
corrected size. True acoustic barge-in is demonstrated in the video and logged
per turn via `ChatMessage.interrupted`.

**The suite cannot run in parallel**, because `/admin/reset` mutates
process-global state in the supplied API.
