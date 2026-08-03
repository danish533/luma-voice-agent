# How the system works

A complete walkthrough for someone who has never seen this repository: what each
piece is, what it is *for*, how a call flows through it end to end, where to
change any given behaviour, and how the local setup differs from a real
deployment.

[README.md](README.md) is the quick start. [ARCHITECTURE.md](ARCHITECTURE.md)
answers the twelve assessment questions. [EVALUATION.md](EVALUATION.md) has the
measurements. **This document is the map.**

---

## Contents

1. [What the system does](#1-what-the-system-does)
2. [The moving parts](#2-the-moving-parts)
3. [The full flow of a call](#3-the-full-flow-of-a-call)
4. [The guardrails, in order](#4-the-guardrails-in-order)
5. [What is stored, and where](#5-what-is-stored-and-where)
6. [Every configuration value](#6-every-configuration-value)
7. [I want to change X — where do I go?](#7-i-want-to-change-x--where-do-i-go)
8. [Local versus production](#8-local-versus-production)
9. [Operating it](#9-operating-it)
10. [What happens when things fail](#10-what-happens-when-things-fail)
11. [How it is tested](#11-how-it-is-tested)
12. [Glossary](#12-glossary)

---

## 1. What the system does

A caller opens a web page, presses **Start call**, and talks. On the other end is
a software agent that behaves like a restaurant's phone receptionist for a
fictional restaurant, **Luma Bistro**.

It does three jobs:

| Job | What that means concretely |
|---|---|
| **Take a reservation** | Ask for date, time, party size, name and phone; check real availability; offer real alternatives if the slot is full; read the details back; write exactly one booking |
| **Change or cancel one** | Find an existing reservation by phone or confirmation code; move it or cancel it after confirming |
| **Fail honestly** | Retry a transient fault once; never invent availability; hand off to a person with the whole conversation summarised |

The restaurant data lives behind a **reservation API** supplied with the
assessment. The agent is a client of that API — it never owns reservation data.

### The one idea worth understanding first

A language model is not trustworthy enough to be given a database. So it isn't.

The model can only act through **seven tools**, and each tool runs a list of
**preconditions** before it does anything irreversible. If a precondition fails,
the tool returns a refusal that the model has to say out loud. The model cannot
book a table that was never confirmed available, cannot book without reading the
details back, and cannot book the same table twice — not because it was told not
to, but because the code will not let it.

That is why the evaluation scores **7/7 on two completely different language
models with identical tool-call sequences**. The reliability is in the
guardrails, not the model.

---

## 2. The moving parts

### Running processes

| Process | Port | Purpose | Lives in |
|---|---|---|---|
| **Agent worker** | 8081 (health), 9091 (metrics) | Registers with LiveKit, answers calls, runs the tools | `src/luma/worker.py` |
| **Ops console** | 8100 | Places the call from the browser; shows what happened | `ops/server.py` |
| **Reservation API** | 8000 | The restaurant's booking system (supplied, unmodified) | `mock_api/app.py` |
| **Postgres** | 5432 | The call record — transcripts, tool calls, handoffs | optional |
| **Redis** | 6379 | Cross-worker idempotency + a short availability cache | optional |

Postgres and Redis are genuinely optional. Unset `DATABASE_URL` and `REDIS_URL`
and every store write becomes a no-op and every cache read a miss, via null
objects in `src/luma/store/null.py`. The agent behaves identically.

### External services

| Service | Role |
|---|---|
| **LiveKit Cloud** | Carries the audio (WebRTC) and dispatches calls to workers |
| **Deepgram Nova-3** | Speech → text, streaming |
| **Deepgram Aura-2** | Text → speech, streaming |
| **OpenAI or Google** | Decides which tool to call and what to say |

### Source layout, and what each file is *for*

```
src/luma/
├── worker.py        entrypoint: register, prewarm, connect, start the session
├── runtime.py       assembles the session — models, turn-taking, barge-in
├── config.py        settings from the environment + restaurant constants
├── prompts.py       the system prompt and the greeting
├── normalize.py     spoken language → API-valid values
├── api_client.py    HTTP to the reservation API: retry, idempotency keys
├── state.py         per-call memory: what was checked, what was created
├── obs.py           JSON logging, PII redaction, latency book
├── metrics.py       Prometheus counters and histograms
├── agent/
│   ├── agent.py     the seven tools
│   ├── guards.py    every precondition, one named function each
│   ├── replies.py   the wording of every refusal and result
│   └── speech.py    filler phrases while a tool runs
└── store/
    ├── models.py    Call, Turn, ToolCall, Handoff
    ├── db.py        background writes to Postgres
    ├── cache.py     Redis idempotency claims + slot cache
    └── null.py      no-op versions of both
```

**Why `guards.py` is separate from `agent.py`.** The safety of this system *is*
the order of the preconditions. When they were inline, that order was buried in
two hundred lines of branching. Pulled out, a tool body reads as a list of gates
and the design is visible at a glance:

```python
if refusal := guards.party_size_is_bookable(size):              return ...
if refusal := guards.not_already_created(...):                  return ...
if refusal := guards.availability_was_verified(...):            return ...
if refusal := guards.caller_has_confirmed(...):                 return ...
if refusal := await guards.no_existing_booking_at_that_time(...): return ...
key = booking_idempotency_key(...)
if refusal := await guards.not_claimed_by_another_worker(...):  return ...
# only now does anything get written
```

**Why `replies.py` is separate.** Wording changes constantly during tuning; logic
should not have to be re-reviewed because a sentence was reworded.

---

## 3. The full flow of a call

### Stage 0 — before anyone calls

The worker starts and does three things that only matter later:

1. **Registers with LiveKit Cloud.** It now appears in the pool of workers that
   can be given a call.
2. **Keeps one job process warm** (`num_idle_processes=1`). Without this the
   first call logs "no warmed process available" and the caller waits several
   seconds in silence — long enough to say "hello? can you hear me?"
3. **Loads Silero VAD once per process**, not once per call. A few megabytes of
   ONNX on the first turn would land directly in the caller's first-response
   latency.

The turn-detector model is imported at **module scope in the main process**. This
looks like a stylistic detail and is not: LiveKit only spawns an inference
executor if a runner was registered before the worker was constructed. Import it
lazily inside the job process and every single turn fails with "no inference
executor", endpointing silently degrades to bare voice-activity detection, and
the agent starts talking over people mid-sentence.

### Stage 1 — placing the call

```
browser → POST /api/token → server mints a JWT scoped to ONE room
browser → joins that room over WebRTC
LiveKit → dispatches the room to a worker
worker  → await ctx.connect()   ← before session.start()
worker  → session.start() → say(greeting)
```

Two details worth knowing:

- **The LiveKit API secret never reaches the browser.** The page receives only a
  short-lived token for a single room.
- **`await ctx.connect()` must come before `session.start()`.** LiveKit's room
  I/O only attaches handlers to an already-connected room. Without it, the agent
  is dispatched, blocks forever, and the caller hears nothing — while every log
  line looks healthy. This bug shipped once; `scripts/smoke_call.py` exists to
  catch it.

While the agent is being dispatched the browser plays a **ringback tone**.
Several seconds of silence reads as "broken"; a ringing cadence reads as "hold
on". It stops the instant the agent's audio arrives.

In parallel, `_prewarm()` warms the HTTP connection and — if Redis is configured
— prefetches open slots for the common party sizes, turning "what have you got on
Saturday?" from six round trips into none.

### Stage 2 — the turn loop

This repeats for every exchange:

**1. Audio in.** Deepgram streams a transcript with interim results, so the
pipeline reacts to partial speech rather than waiting for a final. `numerals=True`
returns `310` rather than `three one zero`, which is decisive for phone numbers.
Keyterm biasing on "Luma Bistro" and "confirmation code" reduces mis-hears.

**2. Has the caller finished?** Two models cooperate:

- **Silero VAD** — is there speech, or silence?
- **The turn detector** — is the *sentence* finished? "Table for four on…" is a
  pause, not a turn.

Both run locally, so there is no network hop on the most latency-sensitive path
in the call.

**3. The model starts early.** With `preemptive_generation` enabled, the LLM
begins on the partial transcript while the caller is still speaking. This is why
the latency legs overlap rather than run in series — and why summing them gives
the wrong answer in *both* directions.

**4. Tool call.** The model picks a tool and fills its arguments. The tool then:

```
normalise  →  guard  →  call the API  →  shape the reply
```

*Normalise* converts what a person says into what the API accepts: "Friday the
fourteenth" → `2026-08-14`, "half six" → `18:30`, "310-555-0147" →
`+13105550147`. *Guard* runs the preconditions. *Shape* returns a status the
model can speak.

**5. Second model round trip.** The tool result goes back to the model, which
turns `{"status": "unavailable", "alternatives": ["17:30", "18:00"]}` into a
sentence. Nothing can start this until the API has answered, which is why it is
the single largest cost on a booking turn and why preemptive generation cannot
hide it.

**6. Audio out.** Aura-2 streams speech back to the caller.

### Stage 3 — interruption

If the caller speaks while the agent is talking, barge-in cancels the in-flight
LLM and TTS work — not merely the speaker. Two thresholds stop a cough from
killing the sentence: speech must last at least `0.5 s` and carry at least
`2 words`. If the interruption turns out to have been noise, the agent resumes
rather than leaving the reply half-spoken.

### Stage 4 — the write

Six preconditions run before a single byte reaches the API. On success:

- the reservation is written with an idempotency key derived from the booking
  itself;
- the confirmation code is read back in NATO alphabet — *"Your confirmation code
  is Luma — Foxtrot, 5, 8, 8"* — because bare letters are unintelligible over a
  phone line;
- the booking is remembered in call state, so an immediate repeat is answered
  without touching the network.

### Stage 5 — afterwards

- Calls, turns, tool calls and handoffs are written to Postgres **in the
  background**. A call must never stall because the analytics database is slow.
- Child writes are gated on the parent `calls` row landing first — otherwise a
  turn can reach the database before the call it belongs to and Postgres rejects
  it on the foreign key.
- The JSONL log is tailed by the console and pushed to the browser over a
  WebSocket.
- Prometheus counters from every job child process are aggregated on scrape.

---

## 4. The guardrails, in order

Each is one named function in `src/luma/agent/guards.py`. All return either
`None` (proceed) or a refusal dictionary (stop, and say this).

| Guard | Stops |
|---|---|
| `party_size_is_bookable` | A party of 20 being sent to an API that will 422 it. Routed to a human instead |
| `not_already_created` | This call booking the same table twice |
| `availability_was_verified` | Booking a table nobody ever checked was free |
| `caller_has_confirmed` | Writing before the details were read back |
| `reservation_is_known` | A misheard id mutating a stranger's booking |
| `no_existing_booking_at_that_time` | The same caller stacking a second booking from an earlier call |
| `not_claimed_by_another_worker` | Two workers racing on a redial (Redis `SET NX`) |

### Duplicate prevention, specifically

Four independent layers, because this is the failure a restaurant actually feels:

1. **A deterministic idempotency key** — `sha256(name|phone|date|time|party_size)`,
   *not* a per-attempt UUID. A UUID per attempt makes the header decorative: every
   retry creates another booking. Deriving it from the booking collapses retries,
   stuttered tool calls and repeated confirmations onto one record, while a
   genuine change of details correctly produces a new key.
2. **An in-call memo** — a repeat is answered from memory, no network call.
3. **A pre-write search** — catches the same caller booking the same slot on a
   *second* call.
4. **The availability gate** — no create without a successful check for those
   exact details. This is also what handles corrections: changing the party size
   invalidates the earlier check and forces a re-check.

> **The trap this defends against.** The supplied API keys its idempotency cache
> on the header **alone and ignores the request body**. Reuse a key with
> different details and it returns the *first* reservation. That is precisely why
> the key must come from the booking rather than from the attempt.

---

## 5. What is stored, and where

### The reservation API owns reservations

The agent never copies reservation data into its own database. `tool_calls`
stores a `reservation_id` and a `confirmation_code` — references, not contents.
Two systems holding the same customer record is two systems that can disagree.

### The call record (Postgres, optional)

| Table | Holds |
|---|---|
| `calls` | One row per call: when, which room, which models, the outcome |
| `turns` | Every utterance both ways, with per-turn latency |
| `tool_calls` | Every tool invocation **including refusals**, with arguments, status and duration |
| `handoffs` | Reason and the summary handed to the person |

The refusal rows are the interesting ones. A spike in
`availability_not_verified` means a prompt change has started pushing the model
toward writes it should not be making.

### Redis (optional), two unrelated jobs

| Key | TTL | Job |
|---|---|---|
| Idempotency claim | 24 h | **Correctness.** `SET NX` so only one worker may write a given booking |
| Slot cache | 90 s | **Latency.** Speeds up *listing* what's open |

**The slot cache is never on the write path.** `check_availability` — the gate a
booking depends on — always hits the API. A booking authorised by a
ninety-second-old snapshot is a booking made on a guess.

### PII

Phone numbers are redacted in logs to the last four digits. The console shows
partial numbers only. Nothing records audio.

---

## 6. Every configuration value

All read from the environment; `.env` at the repo root is the local source.

### Transport and speech

| Variable | Default | Effect |
|---|---|---|
| `LIVEKIT_URL` | — | Your LiveKit Cloud project |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | — | Worker registration and token minting |
| `DEEPGRAM_API_KEY` | — | One key covers both STT and TTS |
| `DEEPGRAM_STT_MODEL` | `nova-3` | Speech recognition model |
| `DEEPGRAM_TTS_MODEL` | `aura-2-thalia-en` | The voice |
| `AGENT_NAME` | `Theo` | What it calls itself — **must match the voice's gender** |
| `TURN_DETECTOR` | `local` | `local` runs in-process (no network hop); `cloud` frees worker CPU |

### The language model

| Variable | Default | Effect |
|---|---|---|
| `LLM_PROVIDER` | `openai` | `openai` or `google` |
| `LLM_MODEL` | `gpt-4.1-mini` | The model |
| `LLM_TEMPERATURE` | `0.3` | Low, because this is tool selection, not prose |
| `LLM_FALLBACK_PROVIDER` | the other one | Hot standby |
| `LLM_FALLBACK_MODEL` | *(empty)* | **Blank disables the fallback.** A standby with no key just 401s on every retry |
| `OPENAI_API_KEY` / `GOOGLE_API_KEY` | — | Whichever providers you use |

### The reservation API

| Variable | Default | Effect |
|---|---|---|
| `RESERVATION_API_URL` | `http://localhost:8000` | Where to book |
| `RESERVATION_API_TIMEOUT_S` | `5` | Per-request timeout |
| `RESERVATION_API_MAX_RETRIES` | `1` | One retry. The caller is waiting in real time |
| `RESERVATION_API_RETRY_CAP_MS` | `1000` | Ceiling on the server's `retry_after_ms` hint |

### Storage, console, observability

| Variable | Default | Effect |
|---|---|---|
| `DATABASE_URL` | *(unset)* | Unset ⇒ no call record, everything still works |
| `REDIS_URL` | *(unset)* | Unset ⇒ no shared idempotency, no slot cache |
| `OPS_USERNAME` | `ops` | Console sign-in |
| `OPS_PASSWORD` | *(generated)* | Unset ⇒ generated per run and printed at startup |
| `OPS_PASSWORD_HASH` | — | Preferred in production: the plaintext never exists in the environment |
| `OPS_SECRET_KEY` | *(generated)* | **Set this**, or every restart drops all sessions |
| `OPS_HOST` | `127.0.0.1` | The container sets `0.0.0.0`; loopback elsewhere is deliberate |
| `OPS_PORT` | `8100` | Console port |
| `LUMA_LOG_DIR` | `logs` | Where JSONL goes |
| `LUMA_LOG_LEVEL` | `INFO` | Log verbosity |
| `METRICS_PORT` | `9091` | Prometheus scrape port |
| `PROMETHEUS_MULTIPROC_DIR` | `.prometheus` | **Required** — see §9 |

### Compose-only (host port overrides)

`POSTGRES_PORT`, `REDIS_PORT`, `API_PORT`, `OPS_PORT`, `METRICS_PORT` — used only
to publish on a different host port when one is already taken:

```bash
POSTGRES_PORT=55433 API_PORT=8001 docker compose up
```

---

## 7. I want to change X — where do I go?

| To change… | Edit | Notes |
|---|---|---|
| **The voice** | `DEEPGRAM_TTS_MODEL` in `.env` | Also set `AGENT_NAME` to match its gender. Audition with `python scripts/voice_samples.py` |
| **What the agent says** | `src/luma/prompts.py` | System prompt and greeting |
| **The wording of a refusal** | `src/luma/agent/replies.py` | Deliberately separate from logic |
| **A safety rule** | `src/luma/agent/guards.py` | One function per precondition; add it to the tool's list in `agent/agent.py` |
| **Add a tool** | `src/luma/agent/agent.py` | Decorate with `@function_tool`; add an endpoint to `api_client.py` |
| **How long before it decides you're done** | `runtime.py` → `turn_handling.endpointing.max_delay` | Currently `1.2 s`. The default `4.0` reads as a dead line |
| **How easily it can be interrupted** | `runtime.py` → `turn_handling.interruption` | `min_duration: 0.5`, `min_words: 2` |
| **Which model** | `LLM_PROVIDER` / `LLM_MODEL` in `.env` | Config change, not a code change |
| **Retry policy** | `RESERVATION_API_MAX_RETRIES` in `.env` | Logic in `api_client.py` |
| **Restaurant hours or dates** | `src/luma/config.py` | `SERVICE_SLOTS`, `BOOKABLE_DATES` |
| **Max party size before handoff** | `src/luma/config.py` → `MAX_STANDARD_PARTY_SIZE` | Currently `8` |
| **Time zone** | `src/luma/config.py` → `RESTAURANT_TZ` | |
| **Cache lifetime** | `src/luma/store/cache.py` | `AVAILABILITY_TTL_S = 90`, `IDEMPOTENCY_TTL_S = 86400` |
| **The database schema** | `src/luma/store/models.py`, then `make migration m="what changed"` | **Always read the generated migration** — autogenerate is a first draft |
| **The console UI** | `ops/index.html` | Single file, vanilla JS |
| **A metric** | `src/luma/metrics.py` | Never put a name, phone or transcript in a label |
| **What the container installs** | `requirements.txt`, then `docker compose build` | |
| **Which services run** | `docker-compose.yml` | |

---

## 8. Local versus production

### What is the same

The container. The image that runs on a laptop is the image that would run on a
server — same Python, same dependencies, same model weights, same code. That is
the main reason to containerise at all.

### What differs

| Concern | Here | A real deployment |
|---|---|---|
| **Reservation data** | A mock API holding state in memory; restarting it is a full reset | The restaurant's real booking system |
| **Scale** | One worker, one region | An autoscaling group keyed on worker load, multi-region |
| **Secrets** | `.env` on disk | A secrets manager, injected at runtime |
| **TLS** | None; plain HTTP on localhost | TLS at the ingress; `Secure` cookies |
| **Logs** | JSONL files in a volume | Shipped to a log store, rotated |
| **Metrics** | Scrapeable, nothing scrapes them | Prometheus + alert rules |
| **Database** | One Postgres container | Managed Postgres with a connection pooler and backups |
| **Deploys** | `docker compose up` | Rolling, with `drain` so in-flight calls finish |
| **Telephony** | Browser WebRTC | LiveKit SIP with a trunk provider for a real phone number |

### What is genuinely missing

These are gaps, not trade-offs — things a real deployment needs that this does
not have, in the order I would close them:

| Gap | Why it matters |
|---|---|
| **No rate limiting on `/login`** | Unlimited password attempts against a console holding customer data |
| **No TLS, cookie not `Secure`** | The session cookie crosses the wire in clear outside localhost |
| **Secrets in `.env`** | Readable by any process on the host; easy to commit by accident |
| **Logs never shipped or rotated** | JSONL grows unbounded; nothing is queryable across workers |
| **No CI** | Nothing runs the 119 tests except someone remembering to |
| **No hash-pinned lock file** | Transitive dependencies float; a compromised release lands unnoticed |
| **No alerting rules** | The metrics exist; nothing pages on them |

`tests/test_worker.py` closes what used to be the top of this list — the
entrypoint had no test, and it is where the silent-agent bug lived. Each
assertion there was verified by reintroducing the bug and watching the right
test fail; a test that has never failed has not been shown to test anything.

---

## 9. Operating it

### Health endpoints

| Endpoint | Kind | Behaviour |
|---|---|---|
| `:8100/healthz` | Liveness | Checks **nothing** downstream. Always 200 if the process can answer |
| `:8100/readyz` | Readiness | Checks the reservation API, logs, Postgres, Redis. **503** if a required one is down |
| `:8081/` | Worker health | LiveKit's own. 503 if the inference process died or the LiveKit connection failed |

**Why liveness checks nothing.** A liveness probe that fails during a dependency
outage gets the container *restarted* for someone else's problem — turning a
degraded reservation API into a crash-looping console. Readiness returning 503
takes the instance out of rotation without killing it, so it recovers on its own.

Postgres and Redis are *reported* by `/readyz` but do not gate it: the console
works without them.

### Metrics — `:9091/metrics`

| Series | Watch for |
|---|---|
| `luma_tool_calls_total{tool,status}` | **The one that matters.** Counts guardrail refusals alongside successes |
| `luma_turn_latency_seconds` | End of caller speech → first audio |
| `luma_turn_leg_seconds{leg}` | Which component to go and fix |
| `luma_duplicates_prevented_total{layer}` | Which layer caught it |
| `luma_reservation_api_*` | Requests, latency, retries |
| `luma_handoffs_total{reason}` | Why calls escalate |
| `luma_barge_ins_total` | How often callers interrupt |

A refusal is not an error. A **change in the refusal rate** means a prompt edit
has started pushing the model toward writes it should not be making — exactly the
failure that stays invisible in logs until someone is double-booked.

**Multiprocess mode is mandatory.** Every call runs in a job child process. A
counter incremented inside a tool lives in that child's memory and vanishes when
the call ends — the parent would report zero tool calls forever while the agent
worked perfectly. `PROMETHEUS_MULTIPROC_DIR` makes children write to shared files
the parent aggregates on scrape.

Latency buckets are tuned for a phone call, not a web request: 1–4 s is the
interesting region. The library defaults bunch everything under one second and
tell you nothing.

### Migrations

```bash
make migrate                       # apply
make migrate-check                 # fail if models and migrations have drifted
make migration m="what changed"    # generate — then READ it
make migrate-sql                   # print SQL instead of running it
```

Alembic owns the schema on any server database. The application only auto-creates
tables on SQLite (dev and test). Letting both own it guarantees drift:
`create_all` builds what the models say today and then skips every existing
table, so a column added in a later migration exists on a fresh database and is
silently missing on an upgraded one.

### Logs

JSONL, one file per call, in `LUMA_LOG_DIR`. Every line has a `session_id` and an
`event`. The console tails them and pushes changes to the browser over a
WebSocket.

---

## 10. What happens when things fail

| Failure | What the system does |
|---|---|
| **Reservation API returns 503** | Retried once, honouring `retry_after_ms` but capped. If it still fails, the agent says so and offers a handoff — never a guess |
| **Reservation API returns 4xx** | Not retried. You asked for something impossible; retrying burns the caller's patience |
| **LLM times out or rate-limits** | The turn is retried on the fallback provider. An LLM outage mid-call is dead air, which is the one place a hot standby earns its complexity |
| **Redis is down** | Every read is a miss, every claim succeeds. Three duplicate-prevention layers remain |
| **Postgres is down** | Writes fail in the background and are logged. The call is unaffected |
| **The caller interrupts** | In-flight LLM and TTS work is cancelled |
| **A cough interrupts** | Below the `min_duration` / `min_words` thresholds, so ignored; if it slipped through, the agent resumes |
| **Party size over 8** | Handoff, before an API call is spent |
| **The model tries to book without checking** | The tool refuses and tells it to check first |
| **The model repeats a create call** | Answered from the in-call memo; no second POST |

---

## 11. How it is tested

| Layer | What it proves | Command |
|---|---|---|
| **119 unit tests** | Guardrails, normalisation, and the worker entrypoint. No LLM key needed | `make test` / `docker compose run --rm tests` |
| **7 scenarios** | The required workflows end to end, scored against **API ground truth** — not the agent's own narration | `make eval` |
| **Smoke call** | The voice path actually carries audio | `make smoke` |

**The smoke call is the one that matters most.** Everything else can pass while
the caller hears nothing at all — that is exactly what a missing `ctx.connect()`
produces. It places a real WebRTC call, speaks a synthesised sentence into the
room, and checks the agent transcribed it, called a tool, and replied.

**Scoring never trusts the agent.** Every scenario ends by asking the API what is
actually on the books: records written, capacity consumed, statuses set. An agent
that narrates a perfect booking it never made fails.

Current results: **7/7, 33/33 checks, 0 duplicate writes on both providers.**

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **Barge-in** | The caller interrupting the agent mid-sentence, and the agent stopping |
| **Cascaded pipeline** | Speech → text → model → text → speech, as separate stages. The alternative is a single speech-to-speech model |
| **E.164** | The international phone format: `+13105550147` |
| **Endpointing** | Deciding the caller has finished speaking |
| **Idempotency key** | A value that makes a repeated request return the original result instead of creating a second record |
| **Interim results** | Partial transcripts emitted while someone is still talking |
| **Preemptive generation** | Starting the model on a partial transcript, before the caller has finished |
| **SFU** | The media server that routes audio between participants |
| **TTFB / TTFT** | Time to first byte (audio) / first token (text) — what matters in streaming, not total duration |
| **Turn detection** | Deciding *semantically* whether a sentence is finished, as opposed to just detecting silence |
| **VAD** | Voice activity detection — is there speech, or silence? |
| **Warm process** | A pre-started worker process, so the first call does not pay for startup |
