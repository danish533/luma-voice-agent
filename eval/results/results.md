| Test | Pass/Fail | Final outcome | Tool calls | Duplicate/wrong write? | End-of-speech to first audio | API latency (p50) | Notes |
|---|---|---|---|---|---:|---:|---|
| T1 | PASS | all checks passed | 2 (check_availability, create_reservation) | no | see voice run | 5.06 ms | Create available reservation |
| T2 | PASS | all checks passed | 4 (check_availability, create_reservation, check_availability, create_reservation) | no | see voice run | 5.93 ms | Unavailable time |
| T3 | PASS | all checks passed | 3 (check_availability, check_availability, create_reservation) | no | see voice run | 7.55 ms | Correction and barge-in |
| T4 | PASS | all checks passed | 3 (find_reservation, check_availability, modify_reservation) | no | see voice run | 6.34 ms | Modify existing reservation |
| T5 | PASS | all checks passed | 2 (find_reservation, cancel_reservation) | no | see voice run | 6.53 ms | Cancel existing reservation |
| T6 | PASS | all checks passed | 1 (check_availability) | no | see voice run | 7.67 ms | Temporary API failure |
| T7 | PASS | all checks passed | 3 (check_availability, create_reservation, create_reservation) | no | see voice run | 6.83 ms | Duplicate protection |

- Model: `google:gemini-3.1-flash-lite`
- Task success rate: **7/7** (100%)
- Check-level pass rate: **33/33**
- Duplicate/wrong writes: **0**
- Text-mode turn latency (LLM + tools, no audio): p50 1655.32 ms, p95 3159.53 ms
- Reservation API latency: p50 6.42 ms, p95 9.9 ms
