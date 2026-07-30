| Test | Pass/Fail | Final outcome | Tool calls | Duplicate/wrong write? | End-of-speech to first audio | API latency (p50) | Notes |
|---|---|---|---|---|---:|---:|---|
| T1 | PASS | all checks passed | 2 (check_availability, create_reservation) | no | see voice run | 7.44 ms | Create available reservation |
| T2 | PASS | all checks passed | 3 (check_availability, check_availability, create_reservation) | no | see voice run | 7.17 ms | Unavailable time |
| T3 | PASS | all checks passed | 3 (check_availability, check_availability, create_reservation) | no | see voice run | 7.48 ms | Correction and barge-in |
| T4 | PASS | all checks passed | 3 (find_reservation, check_availability, modify_reservation) | no | see voice run | 5.19 ms | Modify existing reservation |
| T5 | PASS | all checks passed | 2 (find_reservation, cancel_reservation) | no | see voice run | 5.04 ms | Cancel existing reservation |
| T6 | PASS | all checks passed | 2 (check_availability, check_availability) | no | see voice run | 5.3 ms | Temporary API failure |
| T7 | PASS | all checks passed | 3 (check_availability, create_reservation, create_reservation) | no | see voice run | 7.18 ms | Duplicate protection |

- Model: `openai:gpt-5.4-nano`
- Task success rate: **7/7** (100%)
- Check-level pass rate: **33/33**
- Duplicate/wrong writes: **0**
- Text-mode turn latency (LLM + tools, no audio): p50 3252.38 ms, p95 5707.73 ms
- Reservation API latency: p50 7.17 ms, p95 13.1 ms
