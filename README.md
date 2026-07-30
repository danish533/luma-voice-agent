# Luma Bistro — real-time voice reservation agent

A telephone-style voice agent that takes, changes and cancels reservations for a
fictional restaurant, over WebRTC in the browser. Built on LiveKit Agents with
streaming Deepgram STT and TTS.

It does three things: books a table (checking real availability and offering real
alternatives), finds and modifies or cancels an existing booking, and fails
honestly — retrying transient faults once, refusing to invent availability, and
handing off to a human with the whole conversation intact.

---

## Architecture

```
  Browser ──WebRTC──► LiveKit Cloud ──► Agent worker (Python)
   mic/spk              (SFU)            │
                                         ├─ Deepgram Nova-3 STT  (streaming, interim results)
                                         ├─ Silero VAD + LiveKit turn detector
                                         ├─ LLM (gpt-5.4-nano | gemini-flash-lite) ← tool calling
                                         ├─ Deepgram Aura-2 TTS  (streaming)
                                         │
                                         └─ Tool layer ──HTTP──► Reservation API
                                            normalise · gate · dedupe · retry
```

| File | Responsibility |
|---|---|
| `src/luma/worker.py` | LiveKit entrypoint; VAD prewarm; call lifecycle |
| `src/luma/runtime.py` | Builds the session (models, turn-taking, barge-in) and wires metrics. Shared by the worker **and** the eval suite, so tests exercise what ships |
| `src/luma/agent.py` | The seven tools and the guardrails that constrain them |
| `src/luma/normalize.py` | Speech → API-valid values (E.164, ISO dates, 24-hour times) |
| `src/luma/api_client.py` | HTTP, bounded retry, deterministic idempotency keys |
| `src/luma/state.py` | Per-call state and the handoff summary |
| `src/luma/obs.py` | JSON logging, PII redaction, latency book |
| `tests/` | 81 tests. Guardrails and normalisation, **no LLM key needed** |
| `eval/run_evals.py` | The seven standard scenarios, scored against API ground truth |

Full reasoning, the twelve architecture answers, and a cost model are in
**[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Setup

Needs Python 3.12+ and keys for [LiveKit Cloud](https://cloud.livekit.io) (free),
[Deepgram](https://deepgram.com) (free credit) and one LLM provider.

```bash
git clone <this-repo> && cd luma-voice-agent
cp .env.example .env          # fill in the keys
make install                  # venv, deps, and the turn-detector model weights
```

Then, in three terminals:

```bash
make api        # mock reservation API on :8000  (unmodified, from the starter package)
make agent      # the voice worker
make ops        # the call widget + live console -> http://127.0.0.1:8100
                # `make stop` shuts all three down again
```

Open the console and press **Start call**. (The hosted
[LiveKit playground](https://agents-playground.livekit.io) also works if you
prefer it.)

### The ops console — call widget + live view

`make ops` serves the whole demo on one page at **http://127.0.0.1:8100**:

- a **Start call** button that places the call from the browser over WebRTC —
  press it, allow the microphone, and talk. No playground, no second tab;
- **remaining seats per slot**, so a booking is visibly subtracted;
- a **live event stream** — the conversation itself, plus reservations written,
  duplicates blocked, retries recovered, barge-ins, and per-turn latency;
- **reservation cards** — click one to see who booked, when, party size, notes,
  status history and the idempotency key that made it safe to retry;

The browser plays a **ringback tone** while the agent is being dispatched —
several seconds of silence reads as "broken", and a ringing cadence reads as
"hold on". It stops the instant the agent's audio arrives.

The page holds only a short-lived JWT scoped to one room; the LiveKit API secret
never leaves the server. Each call gets a fresh room, so its logs and metrics
stay separate. The browser SDK is vendored under `ops/vendor/` (refresh with
`scripts/fetch_vendor.sh`) so a flaky network can't break a recording.

Beyond placing the call it is strictly an observer — it never writes to the
reservation API. It also **deliberately does not poll 2026-08-16**: the mock API
returns its one-and-only 503 on the first availability request for that date,
and polling would consume that failure before you could demonstrate it. Append
`?theme=light` or `?theme=dark` to force a mode for screen recording.

Prefer no browser at all? `make console` runs the identical agent against your
terminal's microphone — no transport, no frontend, no signup.

**Changing the voice?** Set `DEEPGRAM_TTS_MODEL` in `.env` (audition candidates
with `python scripts/voice_samples.py`) and set `AGENT_NAME` to match it — a
masculine voice introducing itself as "Ava" is the first thing a caller notices.

**Port 8000 taken?** `make api API_PORT=8010` and set `RESERVATION_API_URL`
to match.

### Windows

The Python is portable — nothing imports a posix-only module, and `uvloop`
excludes itself on win32 through its own dependency marker, so the requirements
resolve cleanly. Only the `Makefile` is Unix-bound: it needs `make`, `sed` and
`pkill`. `make.ps1` mirrors every target for PowerShell.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # if scripts are blocked
.\make.ps1 install

.\make.ps1 api        # terminal 1
.\make.ps1 agent      # terminal 2 — wait for "registered worker"
.\make.ps1 ops        # terminal 3 -> http://127.0.0.1:8100

.\make.ps1 help       # test, eval, smoke, voices, clean-logs, stop, console
```

`.env` carries over unchanged. Add `-ApiPort 9000` to override the port.

Three things the script has to do differently: it reads the port from `.env`
with a regex rather than `sed`, fetches the browser SDK with `Invoke-WebRequest`
rather than `scripts/fetch_vendor.sh`, and stops services by matching their
**command line** rather than their name — all three are `python.exe`, so a
name-based kill would take unrelated Python processes with them.

> **`make.ps1` has not been exercised on a Windows host.** It was written
> against the Makefile's behaviour but never executed there. If you have WSL2,
> prefer it: the Makefile runs unchanged and that is what this project was
> developed and tested against.

If the browser refuses the microphone, use `http://localhost:8100` — Chrome
only grants mic access on a secure origin, and `localhost` qualifies.

### Verifying it works

```bash
make test    # 81 tests + normalisation tests. No LLM key required.
make eval    # the seven standard scenarios end to end. Needs an LLM key.
make smoke   # places a REAL call: speaks a line, checks the agent heard it,
             # called a tool, and replied. Needs `make api` + `make agent`.
```

`make smoke` is the one that catches a broken voice path. Everything can pass
`make test` and `make eval` while the caller hears nothing at all — that is
exactly the failure mode a missing `ctx.connect()` produces.

`make eval` writes `eval/results/results.md` and `results.json`.

### Demo script

Start from a blank slate with `make clean-logs` (resets the API and empties the
event feed), then run `make api`, `make agent` and `make ops`. Put the LiveKit
playground and the ops console side by side — every moment below is then both
audible and visible.

The five required moments, in one continuous call:

| # | Say | What to watch for |
|---|---|---|
| 1 | "Table for four on Friday August 14th at 6 PM." → name, phone, confirm | `check_availability` → read-back → one `create_reservation` |
| 2 | "Actually, book four for 6:30 that same evening." | Refuses to invent; offers 5:30, 6:00, 7:30 — the API's own alternatives |
| 3 | Interrupt mid-confirmation: "Sorry, make that four people." | Agent stops talking immediately; re-checks availability before booking |
| 4 | "Change reservation LUMA-4821 to 7:30 and four people." | `find_reservation` → confirm → `modify_reservation` |
| 5 | "Check Sunday August 16th at 6 PM for two." | 503 on the first call, one retry, honest recovery — visible in the logs |

---

## What makes this reliable

The interesting engineering is not the pipeline — LiveKit provides that. It is
the set of preconditions that hold **regardless of what the language model
decides to do**, all enforced in `agent.py`, all covered by tests.

**No booking without verified availability.** `create_reservation` refuses unless
a *successful* `check_availability` ran for those exact details in this call. The
model cannot book a table it imagined. This also fixes the correction case: when
a caller changes the party size mid-confirmation, the earlier check no longer
matches, forcing a re-check before the write.

**Four layers of duplicate prevention.** A deterministic idempotency key derived
from the booking itself (`sha256(name|phone|date|time|party)`) rather than a
per-attempt UUID — a UUID per attempt makes the header decorative, since every
retry then creates another reservation. Above that: an in-call memo that answers
a repeat without a network call, a pre-write search that catches the same caller
booking the same slot on a second call, and the availability gate. Tested against
the strongest oracle available — remaining capacity at the restaurant.

**Confirmation is a precondition, not a suggestion.** Create, modify and cancel
all require `caller_confirmed=true`, and refuse otherwise with an instruction to
read the details back first.

**Reservation ids must be real.** Any id the API has not shown us this call is
refused, so a misheard or hallucinated id cannot mutate a stranger's booking.

**Failures are values, not exceptions.** Every tool returns a `status` the model
can act on. An exception in a voice agent is dead air; a
`{"status": "slot_unavailable", "alternatives": [...]}` is something to say.

**Fallbacks at every layer that can fail.** A bounded retry on transient API
faults; a second LLM provider on hot standby (`LLM_FALLBACK_MODEL`), because an
LLM outage mid-call is dead air rather than a degraded answer; a phone-number
search that tries every spelling the API might have stored; and human handoff
as the terminal fallback, carrying the whole conversation. Verified by breaking
the primary key mid-call: the turn recovered on the secondary in 2.8 s.

**Alternatives come from the API or not at all.** When a slot is full the tool
passes through exactly what the API returned, and when the API returns none it
says so explicitly — otherwise the model reaches back for a list of *seating
times* it saw earlier and offers them as though they were free.

**"What have you got?" is a first-class question.** `list_availability` probes
the whole evening concurrently and returns only genuinely open times, so the
agent never gropes through the grid one slot at a time.

### The traps in the supplied API, and how each is handled

Working through the starter package turned up several deliberate ones:

| Trap | Handling |
|---|---|
| Phone search matches an exact string, so the seeded `+13105550147` is invisible to a search for `310-555-0147` | Everything is normalised to E.164 before any read or write; search falls back through the other spellings |
| `Idempotency-Key` is a required header, and the cache ignores the request body | Key is derived from the booking's own fields, so it is stable across retries and unique per booking |
| The first `/availability` call for 2026-08-16 returns 503 | Retried once, honouring `retry_after_ms`; if it still fails, handoff — never a guess |
| The README advertises 17:00–22:00 but the data only has 17:30–20:00 | 422 is surfaced as "we don't seat then" with the real service times, not as a crash |
| Party sizes over 8 are a Pydantic 422, not a business error | Routed to handoff before any API call is spent |

---

## Results

Seven standard scenarios, each preceded by `/admin/reset`. Scoring never trusts
what the agent *said* — every scenario ends by asking the API what is actually on
the books: records written, capacity consumed, statuses set. An agent that
narrates a perfect booking it never made fails here.

| Metric | Result |
|---|---|
| Task success rate | **7 / 7 (100%)** |
| Check-level pass rate | **33 / 33** |
| Tool-call accuracy | **17 / 17** |
| Duplicate or wrong writes | **0** |
| Deterministic guardrail suite | **81 / 81** |
| End-of-speech to first audio | **~3.5 s** on a tool-calling turn, measured on a real call |
| Reservation API latency | p50 **8.9 ms**, p95 **12.3 ms** |

Measured on `gemini-3.1-flash-lite` — chosen on latency, not preference. The
`3.6-flash` and `flash-latest` models call tools correctly but average **6–12 s**
per completion because of default extended thinking, and `3.6-flash` rejects
`thinking_budget=0`. Full numbers, the per-leg latency budget and the model
comparison are in **[EVALUATION.md](EVALUATION.md)**.

---

## Known limitations

Stated plainly, because they are the things I would fix next.

1. **`caller_confirmed` is model-asserted.** The tool requires the flag and
   refuses without it, which makes confirmation explicit and auditable in the
   logs — but a determined model could set it without having read anything back.
   A stronger design tracks the read-back utterance in session state and
   verifies the confirmation against the *specific* details spoken. Worth doing;
   not done here.
2. **The service grid is configuration.** With no endpoint listing bookable
   slots, `config.SERVICE_SLOTS` mirrors `seed_data.json`. It is used only to
   suggest what to ask for *after* the API has rejected a slot — never to claim
   a table is free — but it would drift if the restaurant changed its hours.
3. **Call state is in-process.** A worker dying mid-call loses that call's
   context. Correct at this scale; see ARCHITECTURE.md Q9 for what changes.
4. **Barge-in is verified in the voice demo, not in the automated suite.** The
   eval harness is text-mode, so it verifies *correction semantics* — that a
   changed party size invalidates the earlier availability check and produces
   exactly one reservation with the corrected size. True acoustic barge-in is
   demonstrated in the video and logged per turn via `ChatMessage.interrupted`.
5. **English only, and US phone numbers are assumed** when a country code is
   absent.
6. **No telephony.** Browser WebRTC only. LiveKit SIP would add a real phone
   number without touching the agent, but it needs a trunk provider.
7. **The eval suite cannot run in parallel**, because `/admin/reset` mutates
   process-global state in the supplied API.

---

## Scaling

Summarised here, argued in ARCHITECTURE.md Q9. Roughly 4–8 concurrent calls per
vCPU, bounded by the two local ONNX models (VAD and turn detection).

- **10 concurrent** — one 4-vCPU worker, one region. What is here now.
- **100** — 6–12 workers autoscaled on worker load; Postgres for reservations and
  Redis for the idempotency cache; provider rate limits bite before CPU does, so
  quota increases plus `FallbackAdapter` on STT/TTS/LLM; `drain` on deploy.
- **1,000** — multi-region workers routed to the nearest edge; committed
  provider throughput; a circuit breaker in front of the reservation API, because
  retry-once across a thousand calls is a thundering herd; sampled tracing rather
  than full per-turn logs.

Cost is roughly **$0.16 per five-minute call**, dominated by transport and TTS
rather than the LLM — the arithmetic is in ARCHITECTURE.md Q12.

---

## AI assistance disclosure

This implementation was written with **Claude Code (Claude Opus 5)** as a coding
assistant. It was used to explore the starter API's behaviour, draft the tool
layer and tests, and write documentation. Every design decision — the cascaded
pipeline over speech-to-speech, the four-layer duplicate strategy, deriving the
idempotency key from the booking rather than the attempt, the availability gate
as a code precondition, retry-once, and the choice to score evaluations against
API ground truth rather than agent transcripts — was mine, and I can explain and
modify any part of the submission.

The API traps documented above were found by probing the running mock API
directly (`curl` against every endpoint) rather than by reading the README, which
is how the phone-normalisation and documented-hours contradictions surfaced.
