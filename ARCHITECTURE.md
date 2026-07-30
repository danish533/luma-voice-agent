# Architecture

Answers to `ARCHITECTURE_QUESTIONS.md` from the starter package.

---

## 1. Why this voice framework, STT, LLM, TTS and transport?

| Layer | Choice | Why |
|---|---|---|
| Framework | **LiveKit Agents 1.6** | Ships the three things that are tedious and easy to get wrong: a semantic turn detector, barge-in that actually cancels in-flight LLM and TTS work, and per-component metrics tagged with a shared `speech_id`. It also ships a text-mode driver (`session.run()`), which is what makes the evaluation suite in `eval/` possible at all. |
| Transport | **WebRTC via LiveKit Cloud** | WebRTC handles jitter, packet loss and echo cancellation; a raw WebSocket does not. Using the hosted SFU meant no frontend to write and no media server to operate, and it is the same path a production SIP deployment would take. |
| STT | **Deepgram Nova-3**, streaming | Interim results are what make barge-in feel instant — the pipeline reacts to partial speech rather than waiting for a final transcript. `numerals=true` returns "310" instead of "three one zero", which matters enormously for phone numbers. Keyterm biasing on "Luma Bistro" and "confirmation code" measurably reduces mis-hears. |
| TTS | **Deepgram Aura-2**, streaming | Sub-300 ms time-to-first-byte and natural prosody, and it shares the STT key, so the whole speech layer is one vendor and one bill. |
| LLM | **Pluggable; `gpt-5.4-nano` by default, `gemini-3.1-flash-lite` the alternate** | The reasoning job here is small — pick a tool, fill five arguments — so latency and tool-call reliability matter far more than raw intelligence. `LLM_PROVIDER` / `LLM_MODEL` are environment variables, so swapping providers is a config change, not a code change (`runtime.build_llm`). |

The model was chosen by measurement, and the measurement was surprising: the
*newer, larger* flash models are unusable here. `gemini-3.6-flash` and
`gemini-flash-latest` both call tools correctly but average **6–12 seconds** per
completion because extended thinking is on by default, and `3.6-flash` rejects
`thinking_budget=0` outright. `gemini-3.1-flash-lite` returns in ~650 ms with
identical tool-call accuracy on these scenarios. On a phone call, a model that
reasons better but answers three seconds late is the worse model.

**A cascaded pipeline over a speech-to-speech model.** Speech-to-speech would be
lower latency. I chose STT → LLM → TTS anyway because this task is a
transactional workflow: it needs an inspectable transcript for the confirmation
read-back, deterministic tool arguments, and a text-mode replay for testing.
Speech-to-speech gives up all three, and its cost per minute is currently much
higher. Latency is recovered elsewhere — streaming everywhere, preemptive
generation, and a fixed non-generated greeting.

## 2. How is session and reservation state stored?

Two tiers, deliberately.

**Call state** (`state.py:CallState`) is a plain in-process dataclass: collected
details, which availability results were actually verified, reservation ids the
API has shown us, the tool-call log and the transcript. A call is a single
short-lived affair pinned to one worker, so an external store would add a
network hop to every turn and buy nothing.

**Reservation state** is never ours. It lives behind the API, and the API is the
only source of truth. The agent never caches availability to answer a later
question and never reports a booking it did not receive a 2xx for.

The trade-off is honest: if a worker dies mid-call, that call's context is lost.
At the scale in question that is the right trade (see Q9 for what changes).

## 3. How do you cancel generation during barge-in?

LiveKit drives this and I tune it. When the VAD plus the turn detector agree the
caller has started speaking, the session cancels the in-flight LLM stream and
the TTS stream, flushes undelivered audio frames, and truncates the assistant
message in the chat context to only what was *actually heard* — which is the
part that matters, since the model must not believe it said a sentence the
caller never received.

Configured in `runtime.py`:

Configured as one `turn_handling` block rather than the deprecated loose
kwargs — and that is not cosmetic: once a `turn_handling` dict is passed the
individual kwargs are silently ignored, so mixing the two drops whatever you
set.

- `interruption.min_duration=0.5` and `min_words=2` — a cough or an "mhm"
  should not stop the agent mid-sentence.
- `false_interruption_timeout=2.0` with `resume_false_interruption=True` — if
  the interruption produced no actual transcript, resume rather than sit silent.
- `endpointing.max_delay=1.2` — the default 4 s ceiling applies whenever the
  detector is unsure, and on a hesitant caller it is unsure often, so four
  seconds of silence lands mid-booking and reads as a dead line.
- `preemptive_generation` — start the LLM on partial transcripts; a wrong guess
  is discarded, and the win when it is right is a whole round trip. Safe here
  only because the prompt and tool set are fixed for the call; an agent that
  rebuilt either per turn would have to disable it.

Barge-ins are observable: `ChatMessage.interrupted` is logged per turn, and
cancelled LLM/TTS metrics are excluded from latency percentiles so a truncated
turn cannot flatter the numbers.

## 4. How are tool arguments validated?

Three layers, outermost first.

1. **Schema** — `@function_tool` derives a JSON schema from the type hints, so
   the provider rejects structurally wrong calls before they reach us.
2. **Normalisation** (`normalize.py`) — the interesting layer, because callers
   do not speak in ISO 8601. `"310-555-0147"`, `"+1 310 555 0147"` and
   `"three one zero five five five oh one four seven"` all become
   `+13105550147`; `"Friday, August 14th"` becomes `2026-08-14`; `"6:30 PM"`,
   `"7:30pm"` and `"1830"` all become 24-hour times. Failures raise a
   `NormalizationError` carrying a *caller-facing hint*, so the agent asks
   "I need a ten digit number, area code first" rather than "invalid argument".
3. **Business rules** — party size over 8 routes to handoff without spending an
   API call; a reservation id the API has not shown us this call is refused
   outright, which stops a hallucinated or misheard id from mutating a stranger's
   booking.

Deliberately *not* validated: times are never snapped to the 30-minute grid.
6:45 stays 6:45 so the API can reject it and we can offer real neighbouring
slots. Silently booking a different time than the caller asked for is the worst
possible failure here.

## 5. How are duplicate writes prevented?

Four independent layers, because this is the one failure a restaurant actually
feels.

1. **Deterministic idempotency key.** `booking_idempotency_key()` is
   `sha256(name|phone|date|time|party_size)`, not a per-attempt UUID. A UUID per
   attempt makes the header decorative — every retry creates another booking.
   Deriving it from the booking means retries, stuttered tool calls and repeated
   confirmations all collapse onto one server-side record, while a genuine
   change of details correctly produces a new key.
2. **In-call memo.** If this call already created exactly this booking, the tool
   returns the existing confirmation without touching the network.
3. **Pre-write search.** Before creating, search by phone; if that caller already
   holds a confirmed table at that date and time — from an earlier call — offer
   to modify it instead of stacking a second one.
4. **The availability gate.** No create is permitted unless a *successful*
   availability check for those exact details happened first. This also fixes
   the correction case in T3: changing the party size invalidates the earlier
   check, forcing a re-check before the booking can proceed.

Verified by `tests/test_guardrails.py`, including against the strongest possible
oracle — remaining capacity in the restaurant.

> Worth flagging: the supplied API keys its idempotency cache on the header
> alone and ignores the request body, so reusing a key with different details
> returns the *first* reservation. That is why our key must be derived from the
> booking rather than the attempt.

## 6. Which failures are retried?

Retried exactly once: HTTP 500/502/503/504, connection errors and timeouts —
faults where the same request may plausibly succeed. The delay honours the
server's `retry_after_ms` hint, capped at 1 s so a live caller is never left in
silence.

Beyond retry, the LLM has a hot standby: `LLM_FALLBACK_MODEL` wraps the
configured model and its alternate in LiveKit's `FallbackAdapter`, so a 401,
429 or hang switches provider mid-turn instead of leaving the caller in
silence. One sharp edge worth recording: `attempt_timeout` is passed down as a
request deadline and **Gemini rejects anything under 10 s** ("Manually set
deadline 4s is too short"), so a tighter value breaks the fallback rather than
speeding it up. The ceiling only applies to a hang; hard failures switch at
once.

Never retried: every 4xx. A 422 means the request is impossible and a 409 means
the table is gone; repeating either just burns the caller's patience.

The cap is one, not three, and it is deliberate. A caller waiting on a phone line
has a patience budget of roughly two seconds. Exhausting it on retries produces a
worse outcome than a fast, honest "our system isn't responding, let me pass you
to a colleague". When retries are exhausted, the tool returns
`temporarily_unavailable` with `next_step: call transfer_to_human` — it never
returns a guess.

## 7. How is context preserved during handoff?

`CallState.conversation_summary()` builds a human-readable brief and posts it to
`/handoff` along with the caller's phone number:

- every detail collected so far, with "not provided" where there is a gap;
- reservations created during the call, and existing ones found;
- the last eight tool calls with their outcomes — so the colleague sees *what
  was already tried*, not just what was said;
- the last twelve conversational turns.

The goal is that the caller never repeats themselves. The handoff id is logged
and read back, and if `/handoff` itself fails the agent says something true
rather than pretending the transfer worked.

## 8. Which production metrics and logs matter?

Every event is one JSON object on one line tagged with `session_id`, so a call
can be replayed with `jq` and percentiles computed without a metrics backend.

**The number that matters** is end-of-speech to first audio, reconstructed by
joining LiveKit's per-component metrics on `speech_id`:

```
eou_delay + llm_ttft + tts_ttfb
```

Component numbers alone are misleading; a fast LLM behind a slow turn detector
still feels sluggish. Target p95 under 1.5 s.

Also tracked: tool-call outcome distribution (`tool_result` status is the fastest
signal that a prompt change broke something), API latency and status per attempt,
retry and duplicate-prevention counts (`duplicate_prevented` firing in production
means a real double-book was averted), barge-in rate, false-interruption
recoveries, and handoff rate by reason.

Business metrics matter more than technical ones: task completion rate, bookings
per call, handoff rate, and — the honest one — abandonment.

PII is redacted at the logger: `redact_phone()` keeps the last four digits, which
is enough to debug and not enough to identify.

## 9. How would the system change at 10, 100 and 1,000 concurrent calls?

The CPU cost per call is dominated by two ONNX models — Silero VAD and the turn
detector — both of which run locally. Roughly 4–8 concurrent sessions per vCPU.

**10 concurrent.** What is here now. One worker VM (4 vCPU), one region, the
reservation API on a single instance. `prewarm` already loads the VAD once per
process rather than once per call.

**100 concurrent.** Six to twelve worker VMs; LiveKit's dispatcher already
load-balances, so scaling is an autoscaling group keyed on worker load. The
reservation store moves to Postgres with a pooler, and the idempotency cache to
Redis with a TTL so it survives a restart. Provider rate limits become the real
constraint well before CPU does — request quota increases, and wrap STT/TTS/LLM
in LiveKit's `FallbackAdapter` so one provider's bad afternoon degrades quality
instead of dropping calls. Deploys need `drain` so in-flight calls finish.

**1,000 concurrent.** Multi-region workers with callers routed to the nearest
edge; at this size, geography is a latency budget item. Committed-throughput
contracts with the speech providers rather than pay-as-you-go. A circuit breaker
in front of the reservation API — at 1,000 calls, a retry-once policy across all
of them is a thundering herd, so the breaker trips to straight-to-handoff rather
than hammering a struggling service. Backpressure becomes a product decision:
above capacity, an honest "we're busy, please hold" beats degraded audio for
everyone. Logging moves to sampled tracing; full JSON per turn at this volume is
its own cost centre.

The in-process state design survives all three, because a call is pinned to a
worker. What changes at 1,000 is the blast radius of losing one worker, which
argues for faster drain and for writing collected details through early enough
that a redial can resume.

## 10. What would you improve in the supplied API?

Working through it turned up a specific list.

1. **No endpoint lists bookable slots.** Every unknown date or time is a bare
   `422 INVALID_SLOT` with no hint, so an agent cannot answer "what *do* you
   have that evening?" without probing slot by slot. `GET /availability?date=`
   returning the day's grid would remove the one place where this agent falls
   back to configuration (`config.SERVICE_SLOTS`).
2. **Phone search matches on an exact string.** The normaliser strips to digits
   and `+` but never adds a country code, so the seeded `+13105550147` is
   invisible to a search for `310-555-0147` — which is exactly how a caller says
   it. Storing E.164 and matching on the last 10 digits would fix a whole class
   of "I can't find your booking".
3. **The idempotency cache ignores the request body.** Reusing a key with
   different details silently returns the first reservation instead of a `422`.
   The key should be fingerprinted against the payload.
4. **The documented hours contradict the data.** The README advertises
   17:00–22:00; the data only has 17:30–20:00, so a caller asking for the
   advertised opening time gets a hard error.
5. **`409 SLOT_UNAVAILABLE` returns same-day alternatives only.** Nearby dates
   would prevent a dead end when an evening is fully booked.
6. **No authentication, and no `Retry-After` header** (the hint is in the body,
   where standard HTTP clients will not look for it).
7. **State is process-global**, so `/admin/reset` is destructive across
   concurrent test runs — fine for an assessment, but it means the eval suite
   cannot run scenarios in parallel.

## 11. How would you protect PII, recordings, transcripts and secrets?

**In transit and at rest.** WebRTC is encrypted end to end (DTLS-SRTP); the API
moves to TLS with mTLS between the agent and the reservation service. Anything
persisted gets envelope encryption with a KMS-managed key.

**Minimisation first.** The best protection is not storing it. This agent keeps
no audio at all — nothing is written to disk, and no recording is requested from
LiveKit. Transcripts stay in memory for the duration of the call and go only to
`/handoff`, where they are needed for the colleague to do their job.

**Redaction at the boundary.** `redact_phone()` runs inside the logger, not at
each call site, so a new log line cannot accidentally leak a full number. Names
and notes are kept out of logs entirely. Deepgram supports server-side redaction
of numeric entities, which I would enable for a deployment retaining transcripts.

**Retention and rights.** Transcripts with a 30-day TTL and automatic deletion;
handoff summaries tied to the reservation's lifecycle. Under GDPR/CCPA a phone
number plus dining history is personal data, so deletion by phone number has to
be a supported operation, not a database migration. Two-party-consent states
require a recording disclosure in the greeting.

**Secrets.** Nothing but `.env.example` in the repo, `.env` git-ignored, and a
secrets manager with rotation in production — never baked into an image.
Provider keys get per-environment scoping so a leaked staging key cannot touch
production. `logs/` is git-ignored because a transcript is PII.

## 12. Estimate cost per five-minute call

Assumptions: the caller talks for 5 minutes, the agent speaks for roughly 2 of
them (~1,800 characters at conversational pace), and the exchange runs about 12
turns with a growing context.

| Component | Basis | Unit rate (list) | Cost |
|---|---|---|---|
| Deepgram Nova-3 STT | 5.0 streamed minutes | ~$0.0077/min | ~$0.039 |
| Deepgram Aura-2 TTS | ~1,800 characters | ~$0.030/1k chars | ~$0.054 |
| LLM input (flash-class) | ~36k tokens (12 turns, growing context) | ~$0.40/M | ~$0.014 |
| LLM output (flash-class) | ~1k tokens | ~$1.60/M | ~$0.002 |
| LiveKit Cloud | 2 participants × 5 min | ~$0.005/participant-min | ~$0.050 |
| **Total** | | | **≈ $0.16** |

So roughly **15 cents per five-minute call**, or about $0.03/minute — dominated
by transport and TTS, not by the language model, which is the opposite of most
people's intuition and worth knowing before optimising.

Where the money actually is, in order:

- **Transport is the largest single line.** Self-hosting `livekit-server` removes
  it in exchange for operational work; that trade turns positive somewhere around
  the 100-concurrent mark.
- **TTS scales with how much the agent talks.** The terse-replies instruction in
  the prompt is a latency decision *and* a cost decision — halving spoken output
  halves this line.
- **The LLM is nearly free at this size**, and prompt caching on the ~800-token
  system prompt would cut its input cost further. Swapping to Gemini Flash's free
  tier takes it to zero for a demo.
- Telephony, if added via SIP, would add roughly $0.01/min and become the second
  largest line.

Rates are list prices and should be re-checked before anyone budgets against
them; the arithmetic is what matters, and it is reproducible.
