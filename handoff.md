he S12 todo can't run today: its 24-hour window opens tomorrow (Saturday) at 10:40 PST.

**S12** would deploy the Worker to production, run migration 0010, and check the cost drop with `statsctl`:
- **Timing:** it can't start until the window opens tomorrow at 10:40. I can't run anything unattended until then: an in-session wakeup reaches at most an hour ahead, and a cloud agent wouldn't have your Cloudflare credentials. So the practical plan is to start a session after 10:40 tomorrow and say "run S12".
- **Not ready yet:**
  - `statsctl` isn't on your PATH. It runs from `~/d/stats` (`pnpm --dir cloudflare -s statsctl …`) and needs `--credential-file` plus a signed-in Access session.
  - Before starting, run `! cf access login https://stats.17166969.xyz` in that session, and tell me where the stats credential file lives.
- **Approval:** the Cloudflare campaign plan authorized these production writes for S12. Even so, I'll confirm with you right before `make migrate-production` and `make deploy`, because they're outward-facing.
- **Length:** `make check` takes about 10 minutes. `statsctl verify` then waits for a full 24-hour measurement window and can run for up to 48 hours. If it hasn't finished, I'll record the command that resumes it.

**Needs you:**
- For S12, after 10:40 tomorrow: the Access login above and the stats credential file path.
