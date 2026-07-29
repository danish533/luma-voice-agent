| Test | Pass/Fail | Final outcome | Tool calls | Duplicate/wrong write? | End-of-speech to first audio | API latency (p50) | Notes |
|---|---|---|---|---|---:|---:|---|
| T1 | PASS | all checks passed | 2 (check_availability, create_reservation) | no | see voice run | 8.19 ms | Create available reservation |
| T2 | PASS | all checks passed | 3 (check_availability, check_availability, create_reservation) | no | see voice run | 8.57 ms | Unavailable time |
| T3 | PASS | all checks passed | 3 (check_availability, check_availability, create_reservation) | no | see voice run | 9.32 ms | Correction and barge-in |
| T4 | PASS | all checks passed | 3 (find_reservation, check_availability, modify_reservation) | no | see voice run | 8.9 ms | Modify existing reservation |
| T5 | PASS | all checks passed | 2 (find_reservation, cancel_reservation) | no | see voice run | 8.91 ms | Cancel existing reservation |
| T6 | PASS | all checks passed | 1 (check_availability) | no | see voice run | 8.11 ms | Temporary API failure |
| T7 | PASS | all checks passed | 3 (check_availability, create_reservation, create_reservation) | no | see voice run | 7.7 ms | Duplicate protection |

- Model: `google:gemini-3.1-flash-lite`
- Task success rate: **7/7** (100%)
- Check-level pass rate: **33/33**
- Duplicate/wrong writes: **0**
- Text-mode turn latency (LLM + tools, no audio): p50 1616.76 ms, p95 2376.63 ms
- Reservation API latency: p50 8.9 ms, p95 12.32 ms
