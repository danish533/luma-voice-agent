| Test | Pass/Fail | Final outcome | Tool calls | Duplicate/wrong write? | End-of-speech to first audio | API latency (p50) | Notes |
|---|---|---|---|---|---:|---:|---|
| T1 | PASS | all checks passed | 2 (check_availability, create_reservation) | no | see voice run | 6.61 ms | Create available reservation |

- Model: `openai:gpt-5.4-nano`
- Task success rate: **1/1** (100%)
- Check-level pass rate: **5/5**
- Duplicate/wrong writes: **0**
- Text-mode turn latency (LLM + tools, no audio): p50 1528.75 ms, p95 2642.93 ms
- Reservation API latency: p50 6.61 ms, p95 578.04 ms
