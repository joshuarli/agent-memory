Prefix: AGM
Next: 5

### AGM-4 · P1 · Run the live evaluation with the hook arms

- **Area:** Tests
- **Where:** `eval/live_recall.py`, `README.md` (measured results, "Not established")
- **Problem:** `eval/live_recall.py` now has `hook` and `hook_control` arms, but no paid run has used them, so whether hook-injected results change what agents run or whether the task is completed is still unmeasured. Real-world hook data is too thin to answer it (2 attempts by 2026-11-06).
- **Recommendation:** With the owner's approval, run `eval/live_recall.py --live --runs 5` (five arms, 25 Codex runs) and record `hook_better_than_treatment` and `hook_control_clean` in `README.md`, scoped to one model and one task family.
- **Fix with:** none
- **Effort:** S
