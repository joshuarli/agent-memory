# Where usr-memories spends money

The `usr-memories` Worker runs on the Workers Paid plan ($5 a month), and
everything it uses fits inside that plan's allowances. Until 2026-11-05 the
exception was D1 rows written, which `commitSnapshot` drove. This document explains
why it cost so much, what changed, and how to watch it, using the numbers measured
on 2026-11-05.

## Usage against the plan

The figures come from the `stats` dashboard's collected history (about 73 hours,
from 2026-11-01) projected to 30 days. D1 rows written come straight from
`events.d1Queries`. Prices are Cloudflare's list prices on 2026-11-05.

| Component | Used (30-day rate) | Included | Cost over the plan |
| --- | --- | --- | --- |
| Worker requests | 1.5M | 10M | $0 |
| Worker CPU | 10.5M ms | 30M ms | $0 |
| D1 rows read | 1.7B | 25B | $0 |
| D1 storage | 0.12 GB (0.91 GB before 2026-11-05) | 5 GB | $0 |
| R2 storage | 6.1 GB, 95,457 objects | 10 GB-month | $0 |
| R2 operations | 1.25M | 1M Class A, 10M Class B | at most $1.13 |
| D1 rows written | see below | 50M | $1 per million |

R2 operations have no Class A and Class B split in the analytics. The bound
assumes every operation is a Class A `PUT /sync/objects`, which the backfill made
most of them.

## D1 rows written

D1 rows written in November, by UTC day:

| Day | Rows written | What happened |
| --- | --- | --- |
| Nov 1 | 4.2M | backfill |
| Nov 2 | 15.8M | backfill, 10 snapshots |
| Nov 3 | 6.3M | 16 snapshots |
| Nov 4 | 66.4M | every snapshot commit failed |
| Nov 5, to 01:30 | 4.1M | failing, then the mirror was paused |
| Month so far | 96.8M | 46.8M over the allowance |

### A snapshot used to rewrite every object row

`commitSnapshot` inserted one `snapshot_object` row for every object the snapshot
referenced, 32 rows per statement. D1 bills an extra written row for each index a
write touches, and `snapshot_object` had two: its primary key and
`snapshot_object_plaintext`. Each object therefore cost 3 rows written, so a full
`INSERT` statement cost 96. The commit also ran
`UPDATE remote_object SET last_referenced_at = ...` for every referenced digest.

On Nov 3, 16 snapshots of about 75,000 objects each wrote 6.3M rows, about
400,000 per snapshot. At that pace the mirror would have written about 190M rows
a month, about $140 over the allowance. The cost grew with both the object count
and how often learning changed, because a learning change skipped the daily
snapshot interval. The database stayed small (it grew 13 MB on Nov 4) because
retention deleted old snapshots, so storage never showed the churn.

### A failed commit was paid for on every retry

From 2026-11-03 22:22 UTC every `commitSnapshot` batch failed with an error the
Worker reports as `snapshots.commit` in `D1DatabaseSessionAlwaysPrimary._sendOrThrow`,
taking up to 15 seconds before it returned 503. The batch rolled back, so no
snapshot landed, but D1 analytics counted every row it wrote before it failed.
`com.josh.agent-sessions-mirror` runs `agent-sessions mirror sync` every 15
minutes, and a stored error made each run retry the commit. That is the 66.4M
rows on Nov 4, a 30-day rate of about $2,000.

Rolled-back rows are billed. On 2026-11-05 the billing page counted 98.28M D1
rows written for the cycle, which matches the analytics total that includes every
failed commit. The cycle is 48.28M over the 50M allowance, about $48, and the
billing page showed $0 billable usage while the cycle was still open.

## What changed on 2026-11-05

A snapshot's object list no longer lives in D1. The client uploads it in sorted
pages of up to 10,000 `<plaintext_sha256> <size>` lines. The Worker checks each
page against `remote_object` and stores it in R2 at `v1/s/<origin>/<snapshot>/<page>`.
The commit names only the list's count, bytes, and SHA-256, and the Worker checks
them against the stored pages. A commit now writes D1 rows for the snapshot and its
1 MiB database chunks, and resets `gc_pending_at` only on objects that are pending.
GC builds the pinned set from the retained snapshots' pages.

The client also changed how often it snapshots. A learning change waits for the
daily snapshot instead of forcing one. A failed snapshot is retried after 15
minutes, then after double the previous wait, up to a day.

The cutover dropped `snapshot_object` (2.04M rows) and every existing snapshot,
and the database went from 911 MB to 115 MB. The first sync afterwards committed
one snapshot pinning 81,859 objects in 3.4 seconds. That whole sync, including 11
new generations, wrote 2,584 rows. GC then marked 9,006 objects (4.2 GB) that only
the dropped snapshots had referenced, mostly their database chunks, for deletion
after the one-hour grace period, and the sweep at 03:08 UTC deleted them from R2.

## Generation commits write only what is new to the session

After the snapshot fix, a 15-minute sync wrote about 8,000 rows, all from
`commitGeneration`. Each new generation listed every object of its session in
`generation_object`, so a long session rewrote its whole object list on every
sync. Each commit also ran `UPDATE remote_object SET last_referenced_at = ...` on
every object it named, although nothing read that column.

`generation_object` now holds, for each generation, only the objects no earlier
generation of the same session lists. GC reachability is unchanged: it asks whether
any live generation of a session lists an object, and generations are deleted only
by a tombstone, which removes the whole session. Migration 0009 drops
`last_referenced_at`, and a commit resets `gc_pending_at` only on objects that are
pending. The mirror LaunchAgent now runs every hour instead of every 15 minutes,
so an active session makes at most one generation an hour. A session written just
after a run can stay local-only for up to an hour.

## Pausing the mirror

To stop all mirror writes, unload the LaunchAgent with
`launchctl bootout gui/$(id -u)/com.josh.agent-sessions-mirror`. The plist stays
in `~/Library/LaunchAgents`. While it is unloaded no recovery snapshot is taken,
and new sessions stay local-only. To resume, run
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.josh.agent-sessions-mirror.plist`.

## Watching it

Deploy `stats-observe` before the Worker that emits new operation names (see
[operations](operations.md)). The deployed collector validates every
`stats.observe` operation message against `MEMORIES_OPERATIONS`; a name it does
not know, a malformed row account, or a second operation message for one
invocation drops that invocation in stats.

The `stats` project page for `usr-memories` prices D1 rows written per
statement in its cost ledger, and its D1 widget charts rows written per bucket.
`INSERT INTO snapshot_chunk` should cost a few hundred rows a day, at most one
snapshot's chunks. If it dominates the ledger, snapshots are being committed more
often than once a day.

The Worker emits one operation message per instrumented route with the D1 rows
and statement count that request produced. The local contract test asserts the
message names equal the collector's accepted set and that its rows equal the D1
`meta` totals. Rows from a rolled-back batch are still billed by D1 even though
the local `meta.rows_written` only sees the statements that ran.

The fast guard is `make cost-check`: it pins `meta.rows_written` for an object
upload (5), a duplicate generation commit (0), a generation commit that adds no
objects (11), and a snapshot commit (11), and it pins the schema's index set.
The 80,000-object workload is `make perf-cost`; it writes
`.work/perf/cost.json` and `.work/perf/cost.md` with a labeled 30-day projection
priced from `~/d/stats/cloudflare/src/cost/pricing.ts`.

The 2026-11-06 measurement, at 80,000 objects:

| Operation | Rows written | Rows read | R2 Class A | R2 Class B | Duration |
| --- | ---: | ---: | ---: | ---: | ---: |
| Initial backfill | 645,607 | 724,999 | 80,441 | 0 | 178 s |
| 24 hourly incremental syncs | 4,512 | 44,712 | 731 | 0 | 111 s |
| Daily snapshot | 212 | 725,161 | 32 | 9 | 4.8 s |
| GC mark | 0 | 19,758 | 0 | 9 | 0.1 s |
| GC sweep | 0 | 4,541 | 0 | 0 | 0.0 s |
| Tombstone | 17 | 1,025 | 0 | 0 | 0.0 s |
| Learning delta | 18 | 1,423 | 0 | 0 | 0.0 s |
| Full restore | 1 | 244,032 | 0 | 80,960 | 232 s |
| **30-day projection** | **3,900,643** | **69,489,573** | **461,551** | **81,770** | |

The projection prices D1 rows written at $1 per million and rows read at $0.001
per million: $3.9006 and $0.0695, or **$3.9705** for the month before any plan
allowance. The initial backfill and the hourly generations dominate the write
line; the restore dominates Class B reads. The projection is an estimate that
replays one day's pattern over 30 days and keeps the one-time operations once.

The 2026-11-06 S12 follow-up drops the write-side `remote_object_gc` index
(migration 0010). That index only served the GC mark's pending-object scan.
Removing it makes that scan read the table and removes one index write from
every `remote_object` insert and update. The same 80,000-object workload measured
the hourly incremental operation at 4,512 rows written before and 3,984 after
(-11.7%); the initial backfill fell from 645,607 to 565,206 (-12.4%). The 30-day
projection falls from $3.9705 to $3.5300. Rows read rise because the GC scan is
now a table scan, but at $0.001 per million the read change stays below a cent.
The production `statsctl` verification of this policy waits on the 24-hour
window after the S11 deploy and on the deployed Worker.
