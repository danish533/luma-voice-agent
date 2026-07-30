# Evaluation Results

STT **Deepgram Nova-3** · TTS **Deepgram Aura-2** · run 2026-07-29/30.

**The suite was run end to end on two providers, and both scored 7/7 with
identical tool-call sequences** — evidence that the guardrails, not the model,
are what make the agent reliable.

| | `google:gemini-3.1-flash-lite` | `openai:gpt-5.4-nano` |
|---|---|---|
| Task success | **7 / 7** | **7 / 7** |
| Checks | **33 / 33** | **33 / 33** |
| Duplicate / wrong writes | **0** | **0** |
| Harness turn latency p50 | **1,617 ms** | 2,126 ms |
| Harness turn latency p95 | **2,377 ms** | 2,758 ms |
| Reservation API p50 | 8.9 ms | 10.0 ms |

Detailed tables below are the Gemini run (`eval/results/results.json`); the
OpenAI run is in `eval/results/results-openai.json`. Gemini is the faster model
and OpenAI is the safer one to record a demo against — see *Model selection*.

Reproduce with `make api` then `make eval`. Every scenario is preceded by `POST /admin/reset`, as
`standard_test_cases.json` requires. **Scoring never trusts what the agent
said** — each scenario ends by asking the API what is actually on the books:
records written, capacity consumed, statuses set. An agent that narrates a
booking it never made fails here.

---

## Standard test scenarios

| Test | Pass/Fail | Final outcome | Tool calls | Duplicate/wrong write? | End-of-speech to first audio | API latency (p50) | Notes |
|---|---|---|---|---|---:|---:|---|
| T1 | **PASS** | One reservation, Jordan Lee, 2026-08-14 18:00, party 4 | 2 — `check_availability` → `create_reservation` | no | ~3.5 s (measured, tool turn) | 8.2 ms | Exactly one `POST /reservations` |
| T2 | **PASS** | Booked 19:30 for 4 after 18:30 came back full | 3 — `check_availability` ×2 → `create_reservation` | no | ~3.5 s | 8.6 ms | Offered the API's own alternatives; nothing booked at 18:30 |
| T3 | **PASS** | One reservation, 2026-08-15 18:30, **party 4** | 3 — `check_availability` ×2 → `create_reservation` | no | ~3.5 s | 9.3 ms | Correction forced a fresh availability check before the write |
| T4 | **PASS** | `res_existing_4821` moved to 19:30, party 4 | 3 — `find_reservation` → `check_availability` → `modify_reservation` | no | ~3.5 s | 8.9 ms | Patched in place; 18:00 seats released |
| T5 | **PASS** | `res_existing_4821` cancelled | 2 — `find_reservation` → `cancel_reservation` | no | ~3.5 s | 8.9 ms | One cancel call; seats returned to the pool |
| T6 | **PASS** | 503 then success; result reported honestly | 1 — `check_availability` | no | ~3.5 s | 8.1 ms | Statuses `[503, 200]`, 2 attempts, no retry storm |
| T7 | **PASS** | One reservation, capacity consumed once | 3 — `check_availability` → `create_reservation` ×2 | no | ~3.5 s | 7.7 ms | Repeat create returned `already_created` without a second POST |

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

Latency is taken from `ChatMessage.metrics.e2e_latency` — LiveKit's own
wall-clock measure of the gap the caller actually sits through — with the
component legs logged alongside for diagnosis.

### Measured end to end, on a real call

`scripts/smoke_call.py` places an actual WebRTC call, speaks a synthesised
sentence into the room, and reads back the latency LiveKit reports on the
assistant message (`ChatMessage.metrics.e2e_latency`): wall-clock from the
caller falling silent to the agent beginning to respond.

A representative **tool-calling turn** on `gpt-5.4-nano` — the slowest kind,
because the model runs twice, once to emit the tool call and once to speak the
result:

| Leg | |
|---|---:|
| End-of-turn detection | 630 ms |
| LLM time-to-first-token (first call) | 974 ms |
| TTS time-to-first-byte | 607 ms |
| **End-of-speech → first audio (measured)** | **3,549 ms** |

**The legs do not add up to the total, and that is the point.** They sum to
2,210 ms; the missing ~1.3 s is the reservation API call plus the *second* LLM
round trip, which no per-component metric covers. Summing legs is how you
convince yourself a slow agent is fast.

The arithmetic fails in the other direction too. With `preemptive_generation`
the LLM starts *while the caller is still speaking*, so the legs overlap rather
than run in series. An earlier version of this file summed them and reported
~2.3 s; the honest figure for a tool turn is **~3.5 s**, and for a
conversational turn with no tool call it is roughly one LLM round trip lower.
Both numbers now come from the framework, not from addition.

### Components in isolation

`scripts/measure_speech_latency.py`, hitting the providers directly:

| Leg | Mean | Min | Max | n |
|---|---:|---:|---:|---:|
| Deepgram Nova-3 — end of speech to final transcript | 266 ms | 244 | 288 | 4 |
| Deepgram Aura-2 — time to first audio byte | 414 ms | 302 | 890 | 6 |
| `gemini-3.1-flash-lite` — time to first token | 723 ms | 555 | 1,456 | 6 |
| `gpt-5.4-nano` — time to first token | 1,016 ms (p50) | 915 | 2,229 | 6 |

**Isolated benchmarks understate the real thing.** TTS measured 414 ms alone
but ~600 ms inside a call; the LLM ~1,016 ms alone but similar or worse in
place. Contention and the WebRTC path are not free.

Where the time goes, and what would move it:

- **The second LLM round trip is the biggest single cost on a booking turn.**
  Nothing can start it until the reservation API answers, so
  `preemptive_generation` cannot hide it. A conversational turn with no tool
  call is roughly one round trip faster.
- **LLM TTFT is the next largest leg.** Gemini measured ~342 ms faster in
  isolation; switching providers is two commented lines in `.env`.
- **End-of-turn detection is ~630 ms**, mostly the deliberate
  `min_endpointing_delay=0.4` — traded against cutting callers off mid-thought.
- The worker registered in **India West**; co-locating with the caller removes a
  round trip from every leg.

The reservation API is not a factor: p50 **8.9 ms** against a local mock. In
production it would be the one number to watch, since it sits inside the turn.

---

## Deterministic guardrail suite

Separate from the scenarios above, `make test` runs **86 tests** covering
normalisation and the write guardrails directly against the mock API, with no
model in the loop. They are fast (~3 s), deterministic, and need no LLM key,
which makes them CI-appropriate in a way that a model-driven suite is not.

```
86 passed in 8.2s
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

## Model selection

Every candidate was benchmarked rather than assumed. Time-to-first-token with
the real system prompt and tool schemas, n=6, on two turn types:

| Model | Tool turn p50 | Speech turn p50 | Correct? |
|---|---:|---:|---|
| **`gemini-3.1-flash-lite`** | **674 ms** | **597 ms** | yes |
| `gpt-5.4-nano` | 1,016 ms | 905 ms | yes |
| `gpt-5.4-mini` | 1,028 ms | 962 ms | called a tool on a plain hours question |
| `gpt-4.1-mini` | 1,447 ms | — | yes |
| `gpt-4.1-nano` | 842 ms | — | **no — never called the tool** |
| `gemini-3.6-flash` | 6–12 s | — | yes, far too slow |
| `gemini-flash-latest` | ~8 s | — | yes, far too slow |

Four findings worth keeping:

- **Newer and bigger is not better here.** `gemini-3.6-flash` and
  `gemini-flash-latest` call tools correctly but take 6–12 s because extended
  thinking is on by default, and `3.6-flash` rejects `thinking_budget=0`
  outright. On a phone call, a model that reasons better but answers three
  seconds late is the worse model.
- **The fastest model was the wrong one.** `gpt-4.1-nano` had the best latency
  of the OpenAI family and simply never called the tool — it would have
  cheerfully invented availability. Latency is only meaningful once correctness
  holds.
- `gpt-5.4-nano` rejects `reasoning_effort` of `minimal` or `low` when function
  tools are present; only `none` is accepted, and it made no measurable
  difference.
- `gemini-2.5-flash` is retired for new API keys, and there is no non-lite
  `gemini-3.1-flash` text model.

**Which to run.** Gemini is ~340 ms faster per turn, roughly a quarter of the
response budget. OpenAI has no free-tier throttle. The repo defaults to
`gpt-5.4-nano` because a rate limit firing mid-recording is a worse outcome
than 340 ms; swap two commented lines in `.env` for the faster path.

**Barge-in is not covered by the automated suite.** The harness is text-mode, so
T3 verifies *correction semantics* — that changing the party size invalidates
the earlier availability check and yields exactly one reservation at the
corrected size. True acoustic barge-in is demonstrated in the video and logged
per turn via `ChatMessage.interrupted`.

**The suite cannot run in parallel**, because `/admin/reset` mutates
process-global state in the supplied API.
