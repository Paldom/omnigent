Asking 4 harnesses: claude:claude-fable-5@max, codex:gpt-5.6-sol@ultra, grokcli:grok-4.6@xhigh, kimicli:kimi-code/k3 …

## claude:claude-fable-5@max

## 1. Concurrency

**[blocks-shipping] The `acquire()` side effect sits outside every transaction — a crash between `workload.acquire()` and `store.create_run(run)` in `_start` (supervisor module) silently loses a work item.** The module brags that succession is crash-safe because runs and bots commit together, but the item claim is an external effect ("marks its queue line `taken:`") with no compensating record. SIGKILL/power-loss in that window: item marked taken, no run exists, no log. The `ConcurrentTransition` handler right below it is the same bug with a log line — and its message ("a supervisor bug, not contention") is wrong the moment a second run-creating process exists (second supervisor, a `run-now` CLI): both pass `_has_live_run`, both acquire, the index rejects one, and item loss becomes the *common* race, not a bug. Smallest fix: create the run first (pending, no item) to claim the one-live-run slot, acquire after, fail the run if acquire returns none — or give workloads a `release(item)` hook called on refusal.

**[should-fix] The deferred/IMMEDIATE mix is safe only under conditions you haven't pinned.** Write-first single statements on a deferred connection go straight for RESERVED, so `busy_timeout` covers them — *if* it's set on the CLI's connections too (nowhere visible in `BotStore`). The unsafe shape is read-then-write inside one deferred txn: in journal mode that's an immediate, timeout-ignoring `SQLITE_BUSY` (deadlock avoidance) when the supervisor holds RESERVED via `BEGIN IMMEDIATE`; in WAL it's `SQLITE_BUSY_SNAPSHOT`. Nothing shown does that (`revise()` reads-then-writes but via `atomic()`), so verify `Store`: especially that `Store.transition(conn=conn)` genuinely executes on the passed connection. If it opens its own deferred txn on a *second* connection, `_transition` self-deadlocks — every settle times out, no run ever settles, the whole fleet freezes.

**The `nullcontext` question: I checked every joined call and no shown path relies on an inner commit** — `_transition` and `_back_off` swallow `ConcurrentTransition` *outside* their `atomic()` blocks, so rollback is intact. Residual hazard only: `create()` converts `IntegrityError` → `ConcurrentTransition` while joined; a future caller that swallows it and commits keeps the already-applied bot INSERT.

Note the bot is read *outside* the txn in `_transition`; a CLI write in the gap fails the bot CAS and rolls back the run's transition too. That's correct (retried next tick) but the base handler will attribute the `ConcurrentTransition` to the run, which it wasn't.

## 2. Arithmetic

- **[should-fix] rrule drift.** `next_occurrence` anchors `dtstart` to *now* on every settle. Any rule without absolute BY-parts (`FREQ=DAILY`, `FREQ=HOURLY`) re-phases to the settle instant: bot due 09:00, runs 20 min, settles 09:20 → next fire 09:20 tomorrow. "Daily at 9" drifts by one runtime per iteration, forever. Fix: persist the activation-time dtstart in `WakePolicy` and anchor to it.
- **[should-fix] Finite rrule runs out → false alarm loop.** `UNTIL` in the past makes `next_occurrence` return `None` → `Wake(None, SCHEDULE)` → `stalled()` fires *every tick* claiming "succession was lost" and prescribing `army bots wake` — which runs it once and re-wedges it. Fix: `None` occurrence → `exhausted=True`, pause with a real reason.
- **[should-fix] RRULE bots are exempt from idle exhaustion** (`MAX_IDLE_STREAK` check is `CONTINUOUS`-only in `next_wake`). A valid `FREQ=MINUTELY` bot with an always-false precondition polls at full rate forever — the quota loop the module docstring forbids, from one config string.
- **[should-fix] RATE_LIMITED with no identifiable vendor = zero backoff anywhere durable.** `next_wake` returns `Wake(now, …)` trusting the gate, but `_cool_vendor` no-ops when `rate_limited_harness` is absent, and `_open_run`'s gate check requires `bot.harness is not None`. Redispatch every tick against a 429ing vendor, streaks never grow (RATE_LIMITED preserves both), never pauses. Fix: no harness → treat as RETRYABLE_ERROR for scheduling.
- [nit] `kwargs.get("now") or int(time.time())` in `_transition` treats a deliberate `now=0` as "read the clock". Clock-backwards is benign (earlier wakes, streaks intact); `_capped` is overflow-safe but O(streak) when `factor==1.0` — bounded only because streak caps exist.

## 3. State machine

- **[should-fix] `classify()` keys on target alone, so every arrival at READY is "the vendor refused on quota".** `PAUSED` is in `_SETTLING` ("a human stopped this branch"), so a resume path must exist, and its natural target is READY → operator resume records `last_outcome=rate_limited`, sets the bot due *now*, and settles a third time when the run finally completes — three `record_wake`s for one logical iteration. Similarly, operator-cancel→FAILED counts as RETRYABLE_ERROR: five cancels pauses the bot as "exhausted". Fix: classify on (source, target) or an explicit requeue-reason artifact.
- **[should-fix] NO_WORK hard-resets `error_streak` to 0 — including `_back_off`'s path, which never touched the vendor.** A bot whose vendor call always fails but whose precondition usually says "empty" alternates error(+1)/idle(reset) and never reaches `MAX_ERROR_STREAK`: it pays for a failing vendor turn forever, never pauses, never alarms. Fix: preserve `error_streak` on NO_WORK (or reset only when the no-work came from a *completed run's* declaration).
- [nit, verify] `record_wake` binds `bot.last_outcome` (the enum, not `.value`) when `outcome is None` — the `wake_now` path. If `RunOutcome` isn't a `str` subclass, that's an `InterfaceError` on every manual wake of a bot with history.

## 4. Escape hatches

- **[should-fix] Every auto-pause exits the alarm surface.** `stalled()` filters `status=ACTIVE`, so a bot paused by a *transient* `WorkloadRefused` (bad deploy, import hiccup) stays PAUSED silently after the deploy is fixed. One log line at pause time is the only trace; you notice weeks later when someone asks why the bot stopped. Same for UnknownPrecondition and streak-exhaustion pauses. Fix: a `paused_reason`/`paused_by=system` column, surfaced by `fleet_tick` alongside stalled.
- **[should-fix] `_ask`'s transient-OmniError `return None` has no cap.** Omni down all weekend = `evaluate()` re-run every tick (workload-defined, possibly not free), run wedged in EVALUATING, bot excluded from both `due()` and `stalled()` (it has a live run) — no alarm anywhere. Fix: after N transient failures, park on the durable row like the non-transient branch.
- The `_back_off`/`_pause`/`_retire_if_expired` swallows themselves are fine — CAS-fail leaves `next_due_at` untouched, so each converges on the next tick. The stranded-item log in `_start` is the exception: log-only record of real data loss (and `%r` on `item` can dump a megabyte payload into the log).

## 5. Omissions

- **[should-fix] No index for terminal runs by bot.** The partial unique index covers only *live* rows; `runs_for()` (`WHERE bot_id=? ORDER BY created_at DESC`) full-scans `runs`, which grows one row per iteration with **no retention policy** — 40 continuous bots ≈ thousands of rows/day, so every ledger view degrades monthly. Fix: `CREATE INDEX ix_runs_bot ON runs(bot_id, created_at)` plus a prune.
- [should-fix] `stalled()` alarm has no dedup — the same lost bot logs `ERROR` every tick until fixed: a 3am log storm, then a muted alert.
- [should-fix] No `PRAGMA busy_timeout`/WAL anywhere in shown setup; if `Store` doesn't set them, two processes means sporadic "database is locked" under any overlap.
- [nit] `create()` maps *all* `IntegrityError` to "slug already exists" — a NOT NULL bug reads as a duplicate at 3am. `provider_gates` rows accrete forever. rrule `BYHOUR=9` is UTC while operators will read it as local — undocumented.

Biggest three if you fix nothing else: the acquire-outside-the-transaction item loss (§1), target-only `classify` (§3), and rrule drift (§2).

Unverified assumptions (Store and base Supervisor not shown): that `Store.transition` honors `conn=`, that the base tick catches `ConcurrentTransition` from `_transition`, and whether a PAUSED→READY resume path exists — each flips a finding between should-fix and blocks-shipping.

## codex:gpt-5.6-sol@ultra

Do not ship this as written. `BEGIN IMMEDIATE` protects settlement writes, but several decisions and side effects occur outside that fence.

1. **Concurrency — blocks-shipping.** In `army/bots/supervisor.py: BotSupervisor._start`, this interleaving defeats pause: supervisor reads bot ACTIVE/due; CLI pauses it and commits; supervisor uses the stale bot, sees no live run, irreversibly calls `workload.acquire()`, then successfully creates a run. The paused bot now runs. Two openers similarly acquire two items before the partial unique index rejects one, permanently stranding the loser’s item. Fix with a durable `ACQUIRING` reservation inserted transactionally using a guarded `INSERT … SELECT` that checks bot version/status/due/live-run; finalize it idempotently after acquisition, with recovery/release for failed reservations. Separately, `_transition` reads the bot before `BEGIN IMMEDIATE`: after a non-idempotent `apply()`, CLI can update bot version, making `record_wake` fail and roll back the run while the external action remains; the next tick repeats it. Begin first, read the bot through that connection, then transition/wake. `_cool_vendor` must also move inside that transaction: currently a losing run/bot CAS still leaves a committed global cooldown. A truly write-first, single-statement deferred CAS is safe from lost updates, although it can raise `SQLITE_BUSY`; deferred read-then-write methods are not. `BotStore._tx(nullcontext(conn))` is correct—none of the shown nested paths should commit independently.

2. **Arithmetic — blocks-shipping.** In `army/bots/schedule.py: next_occurrence`, `FREQ=MINUTELY;COUNT=1` without `DTSTART` returns `None` before the bot’s first run, while `COUNT=2` is re-anchored at every completion and therefore never exhausts. Persist a stable recurrence anchor and last-fired occurrence. Without that high-water mark, a clock rollback from 10:00 to 09:59 schedules the already-fired 10:00 occurrence again. Worse, `DTSTART:19700101T000000Z\nRRULE:FREQ=SECONDLY` can monopolize the loop because dateutil’s `after()` iterates occurrences from the anchor until one passes the requested time ([dateutil source](https://github.com/dateutil/dateutil/blob/master/src/dateutil/rrule.py)). Restrict frequency/anchor age and evaluate arbitrary rules with a cancellable deadline. Also: `_capped(..., 900)` followed by jitter `0.5` can return 1,349 seconds, so the documented ceiling is false; cap after jitter. Validate finite numeric fields and checked epoch addition—e.g. continuous `now=2**63-10` overflows SQLite’s integer range. Wrap both `datetime.fromtimestamp` and `.after`, not merely `rrulestr`. Finally, `_transition`’s `kwargs.get("now") or time.time()` mishandles the valid input `now=0`; use an explicit `is None` test.

3. **State machine — blocks-shipping.** `classify()` says workloads choose only “work done” versus “no work,” but accepts every `RunOutcome`. A legal `COMPLETED` or `CONTINUE` transition carrying `artifacts["outcome"]="blocked"` makes the run terminal while setting the ACTIVE bot’s `next_due_at=NULL`; it sleeps forever and `fleet_tick` only emits repeated stalled alarms. Whitelist `{WORK_DONE, NO_WORK}` for workload-decided terminal moves. Also, `RATE_LIMITED → Wake(now)` is unsafe when no durable gate exists, and even with a gate the existing `READY` run is excluded from `due()` yet included by `_active_runs`; with `lanes=None`, or after restart clears the RAM lane, it retries immediately and bypasses `blocked_vendors()`. Admission for active `READY` runs must consult the durable gate. Streak semantics are also not actually consecutive: `idle_streak=11 → FAILED → NO_WORK` reaches 12 and pauses after only one consecutive empty result because errors preserve the idle streak. Reset it on error or rename/document the counter.

4. **Escape hatches — should-fix, with one blocks-shipping case.** `_retire_if_expired` logs “retiring,” suppresses a failed CAS, and returns `True` even though the row remains ACTIVE; log only after success, reload on conflict, verify the postcondition, then retry or return false. `_pause` and `_back_off` normally lose only one tick, but DEBUG-only suppression can hide persistent contention and create a hot due loop; suppress only when a reload proves the winning write already established an acceptable state. The permanent loss is `_start`’s `ConcurrentTransition` handler: it knowingly returns after acquisition without compensating release. Expiry is also checked only for due bots, so ON_MESSAGE/MANUAL bots remain ACTIVE indefinitely and live runs can continue past TTL; add an independent indexed expiry sweep and define cancellation for live runs.

5. **Missing safeguards — should-fix.** `_open_run` can starve unrelated vendors: if its first 50 due rows belong to a blocked vendor, bot 51 on an open vendor is never examined until that gate clears. Exclude active provider gates in the SQL or page until the bounded number of eligible bots is examined. The shown schema also lacks `(parent_bot_id, created_at)` for `children()`, making recursive retirement/fan-out O(n²); add it. Verify that `runs_for()` has `(bot_id, created_at DESC, id DESC)` in the omitted runs schema, and paginate `revisions()`. Required regression tests are barrier-controlled pause-versus-start, duplicate acquisition, bot-CAS after external apply, losing rate-limit CAS, finite/old-anchor RRULEs, clock rollback, active READY after restart, expired event/live bots, and every swallowed-CAS postcondition.

## grokcli:grok-4.6@xhigh

I'll review the scheduler for real bugs, starting from the concurrency and succession paths rather than the happy-path comments.MCP isn’t available here, so I’ll locate the scheduler files on disk and read them directly.I'll try a filesystem search for the army scheduler sources so the review can cite real interleavings, not just the pasted snippet.The deferred/immediate mix and the READY “settling” path are the two places this will actually lose work or spin a vendor. The rest is how those failures stay invisible.## 1. Concurrency

**The DEFERRED/IMMEDIATE mix is not safe.** Classic SQLite deadlock, not a theoretical one.

Interleaving:

1. CLI `army approve` (or any `Store.transition` / `create_run` on the old connection) starts a **DEFERRED** tx and `SELECT`s the run — SHARED lock.
2. Supervisor `_transition` / `_back_off` does `BEGIN IMMEDIATE` — RESERVED. Coexists with SHARED.
3. CLI `UPDATE runs … WHERE version=?` needs RESERVED — waits.
4. Supervisor `COMMIT` needs EXCLUSIVE — waits on the CLI’s SHARED.

SQLite returns `SQLITE_BUSY` to one of them. Default `busy_timeout` is 0, so it fails immediately. Symptom: the tick dies or the CLI errors with “database is locked”; if the supervisor’s exception handler treats it as `ConcurrentTransition` and moves on, this tick’s succession did not happen.

**Smallest fix (blocks-shipping):** every writer, including single-statement `create_run` / `transition` / command consume, uses `BEGIN IMMEDIATE`. Set `PRAGMA busy_timeout=5000` on every connection. Do not keep a deferred connection “for simple statements.”

---

**Inner commit: `BotStore._tx()` is correct; `Store.transition` is the hole.** Nested `record_wake(..., conn=conn)` + `set_status(..., conn=conn)` do **not** need an inner commit — `record_wake` mutates `bot.version` in memory before `set_status` CAS, so the second write matches. That part is fine.

What is not fine is `_transition` calling `self.store.transition(..., conn=conn)` **without going through `super()._transition`**. If `Store.transition` still commits on its own connection (the “single-statement deferred” path) — or if `conn=` is accepted but the method still `commit()`s — the run move commits, then `record_wake` is a second transaction. Crash or BUSY in the gap:

- Run is `completed` / `failed` / `continue` (not live).
- `next_due_at` still `NULL` or still the *previous* due time.

`NULL <= now` is false in SQLite, so a bot that was unscheduled (post-`WAITING_HUMAN`) **never appears in `due()`**. That is the “lost forever” case the docstring describes, and the one-transaction rewrite does not actually close it unless `Store.transition` joins the `BEGIN IMMEDIATE` and does not commit.

If `next_due_at` was still in the past, `due()` immediately opens a **second iteration of the same work**.

**Smallest fix:** `Store.transition` must take `conn`, execute on that connection, and **not commit**. Same for command consume. Assert in a test that `BEGIN IMMEDIATE` + kill-after-run-UPDATE + restart leaves either both writes in or both out.

---

**CLI pause vs supervisor succession (lost update on the bot row).**

1. Supervisor reads `bot` at version `N` (`_transition` / `_back_off`).
2. CLI `set_status(PAUSED)` — version `N+1`, `next_due_at = NULL`.
3. Supervisor CAS on `record_wake` fails. If that exception rolls back the **whole** IMMEDIATE tx, the run stays un-settled — OK.
4. If `store.transition` already committed (hole above), the run is terminal, pause won, bot has `next_due_at IS NULL` and **no live run**. Human thinks they paused it; `stalled()` fires only if `wake_reason` is not `event`/`manual`. For `ON_MESSAGE` bots the alarm is blind (see §5).

Worse, the other order: run is already live, CLI pauses the bot (status only), `_active_runs` still advances the run, settling `record_wake` **writes a due time back onto a PAUSED bot** (it CASes status-agnostically). Roster shows a paused bot about to run; `due()` ignores it (`status = ACTIVE`). Unpause without `first_wake` then uses that leftover due time.

**Smallest fix:** `record_wake` CAS must include `AND status = 'active'`, or `set_status(PAUSED)` must be the only writer of `next_due_at` and succession must no-op if status ≠ ACTIVE.

---

`_start` is also a real TOCTOU, not just a comment: `_has_live_run()` (one IMMEDIATE read of all live runs) → destructive `acquire()` → deferred `create_run()`. CLI or a second tick can insert the live run in between. Unique index saves the invariant; **the work item is stranded**. That is a wrong result, loudly logged. Still a concurrency bug.

---

## 2. Scheduler arithmetic

**`next_occurrence` replays on a backward clock (should-fix).**

`dtstart=now` and `after(start, inc=False)` means the rule is re-anchored to whatever `now` is. Input: rrule `FREQ=DAILY;BYHOUR=9`, last successful wake at `T=09:00`. NTP (or a test/`now=` argument) sets `now` to 08:50. `next_wake` returns **today 09:00 again**. Symptom: the same daily slot runs twice. Same for any finite `UNTIL`/`COUNT` still in the future relative to the jumped clock.

**Smallest fix:** persist `dtstart` (activation time, or last *scheduled* fire, not last finish) on the policy/row. Pass that to `rrulestr`. Coalesce with `after(max(stored_dtstart, now))`.

---

**`factor <= 1` (or `0`) makes error/idle delay collapse (should-fix).**

`_capped(30, streak-1, policy.factor, 900)`: if `factor` is `0` or `0.5`, `delay *= factor` goes to 0, never hits the ceiling, returns `0`. `now + 0` → due this tick. A flapping vendor becomes a hot loop. Negative `factor` produces a **negative** delay (`int(min(-n, 900))`), `next_due_at` in the past, same symptom.

Jitter does not save you: `_jittered` is a no-op on `delay=0`.

**Smallest fix:** reject `factor < 1` (and `min_interval_s < 1` for `CONTINUOUS`) in `WakePolicy`. Floor delay at 1s after `_capped`.

---

**Idle cap is CONTINUOUS-only; rrule `SECONDLY` never backs off (should-fix).**

`NO_WORK` increments `idle_streak` for every kind, but `streak >= MAX_IDLE_STREAK` only pauses `CONTINUOUS`. A valid `FREQ=SECONDLY` (or `MINUTELY`) bot with a failing precondition wakes **every occurrence forever**. `idle_streak` grows without bound; `_capped` is not even called on the rrule path.

Finite rules (`COUNT=1`, `UNTIL` in the past): `next_occurrence` returns `None`, `Wake(None, SCHEDULE, …)`. Bot is ACTIVE, unscheduled. `stalled()` will alarm (reason is `schedule`, not `event`/`manual`) — good — but `first_wake` of an already-exhausted rrule activates a bot that can never run.

**Smallest fix:** apply the idle cap regardless of kind; treat `next_occurrence is None` as exhausted and pause.

---

**Jitter is applied after the cap, so `RETRY_MAX_S` / `policy.max_s` are not ceilings (nit).** `_jittered(900, 1.0)` can return 1800. Symptom: “15 min error cap” is actually up to 30 min. Apply jitter inside the cap, or cap again after jitter.

**`now + min_interval_s` with `min_interval_s=0` on `WORK_DONE`** is the quota loop the module docstring warns about, and `classify()` defaults to `WORK_DONE`. That is arithmetic + state machine together.

---

## 3. State machine

**`READY` in `_SETTLING` is the illegal succession (blocks-shipping).**

`classify(…, READY) → RATE_LIMITED` → `next_wake` → `Wake(now, …)` → `record_wake` + `_cool_vendor`. But READY is **still a live run** (unique index and `due()` only treat `continue`/`completed`/`failed` as dead).

What actually happens:

1. Vendor refuses; run is requeued `→ READY`. Succession fires. `last_outcome=rate_limited`, `next_due_at=now`, `provider_gates` written.
2. `due()` will not open a new run (live READY row).
3. `_active_runs` still has that run. The next tick **dispatches the same READY run**. `_open_run`’s `blocked_vendors` check is never consulted for in-flight runs.
4. Process restart: in-memory `Lane.cooldown_until` is empty (they know this — that is why `provider_gates` exists). READY run is live, so the durable gate is bypassed. **Quota spin they thought they fixed.**
5. When the run later `COMPLETED`, succession fires **again**. Streaks happen not to double-increment (`RATE_LIMITED` preserves them), but `last_outcome` and `next_due_at` from step 1 were a lie about the iteration having ended.

READY cannot both “settle the iteration” and remain the live iteration.

**Smallest fix:** take `READY` out of `_SETTLING`. Cool the vendor only. Hold dispatch until `blocked_until` (and in-memory lanes) say so. Do not `record_wake` for a requeue. If you want rate-limit to end the iteration, transition to a **terminal** state and re-acquire when the gate opens.

---

**Default `WORK_DONE` desyncs streaks (should-fix).**

`COMPLETED`/`CONTINUE` with missing or unknown `artifacts["outcome"]` → `WORK_DONE` → both streaks reset, continuous bot wakes at `min_interval_s`. A workload that forgot to declare `no_work` never backs off and never hits `MAX_IDLE_STREAK`. Symptom: bot looks healthy, hammers the vendor, idle pause never trips.

**Smallest fix:** fail closed to `NO_WORK` (or refuse to settle and keep the run in `EVALUATING` with an error log that includes `bot.slug`).

---

**Paused *run* vs paused *bot*:** run `→ PAUSED` classifies `BLOCKED`, `next_due_at=NULL`, run **stays live**. `stalled()` requires no live run. Bot is ACTIVE, unscheduled, with a parked run, **no alarm**. Same for a WAITING_HUMAN run that the CLI completes via raw `Store.transition` (skips `BotSupervisor._transition`): succession never runs; `wake_reason=human` does at least hit `stalled()`.

**Smallest fix:** CLI approve/resume must use the same IMMEDIATE succession tx. Treat a `PAUSED` run as either bot-PAUSED or stalled.

---

**`_workload_for` uses the live bot, not `run.revision_id`.** Operator revises `workload` mid-flight: dispatch ran A, collect/evaluate/apply run B. That is an inconsistent iteration, and it is the documented behavior.

**Smallest fix:** resolve workload from the pinned revision.

---

## 4. Escape hatches

**`_cool_vendor` silent no-op (blocks-shipping, hides the real fault).**

```python
harness = str(run.artifacts.get("rate_limited_harness") or "")
if not harness:
    return
```

If the workload reports a rate limit without that artifact, no durable gate, no log. Combined with §3, reboot → every READY bot on that vendor fires at once. You notice as a vendor outage, not a scheduler line.

**Smallest fix:** `_logger.error` and skip dispatch of that run until an operator sets a gate; never `return` quietly.

It also commits **before** the succession tx. If `_cool_vendor` succeeds and succession BUSY-fails, you have a gate with no corresponding run state (mostly harmless, MAX-extends). If you ever move it after the tx, the reboot hole gets worse.

---

**`_retire_if_expired` returns `True` even when the write did not happen (should-fix).**

```python
with suppress(ConcurrentTransition):
    self.bots.set_status(bot, BotStatus.RETIRED, now=now)
return True
```

CAS fails (CLI `wake_now` / revise bumped version) → we still skip `_start`. One missed tick if the bot is still ACTIVE and expired; we retry retire next tick. Not a permanent loss **unless** the competing write was a wake that expected a run this tick and the expiry is wrong (clock jump). `IllegalBotMove` is **not** swallowed — a double-retire that `due()` should not produce would kill the tick.

**Smallest fix:** return whether `set_status` succeeded; on `ConcurrentTransition`, `continue` without claiming retirement (re-read next tick). Catch `IllegalBotMove` too.

---

**`_pause` / `_back_off` swallow `ConcurrentTransition` at DEBUG (should-fix).**

Unknown precondition: `_pause` fails, bot stays ACTIVE, you do get an ERROR every tick from `_has_work` — visible.

Empty wake: `_back_off` fails, `next_due_at` stays in the past, `_has_work` retries every tick. If the winner was `wake_now`, you **skip the idle backoff** and poll at full rate. Log level DEBUG: at 3am this is “bot spinning” with no line.

**Smallest fix:** log WARNING with `bot.slug`, versions, and intended `next_due_at`. Do not swallow `OperationalError`/`BUSY` as contention.

---

**`create_run` unique-index handler (should-fix):** correctly refuses to pretend this is contention, but `_open_run` then sets `opened = None` and **starts the next bot**. You have one stranded queue item *and* another run. Smallest extra fix: on that path, `opened = _SENTINEL` so the tick does not also open work, or nack the item in the same function.

---

## 5. What is missing that will bite

| Gap | Symptom | Severity | Smallest fix |
|---|---|---|---|
| Dispatch of live READY runs never reads `provider_gates` | After restart, rate-limited runs immediately hit the vendor | blocks-shipping | Gate check in the READY dispatch path, not only `_open_run` |
| `record_wake(..., outcome=None)` binds `bot.last_outcome` (an enum) | `wake_now` after the first iteration raises `ProgrammingError` / stores garbage; CLI cannot restart a blocked bot | blocks-shipping | `None if outcome is None else outcome.value` — and do not write `last_outcome` when outcome is None |
| `scan_limit` + blocked vendors do not advance `next_due_at` | 50 bots on one cooling vendor occupy the whole `due()` page; bot 51 never wakes | should-fix | Filter `harness NOT IN blocked` in SQL, or skip blocked bots without counting against `LIMIT` |
| `stalled()` treats `EVENT`/`MANUAL` as legitimate forever | Lost succession on `ON_MESSAGE` is indistinguishable from “waiting for a message”; no 3am line | should-fix | Alarm if `updated_at` is older than some SLA, or if an unread message exists |
| `classify` warning uses `run.id[:12]`, no slug | Cannot map a bad outcome to a bot when paging logs | should-fix | Log `bot.slug` |
| `runs_for` / history has no `(bot_id, created_at)` index | Ledger walk becomes a table scan as `runs` grows; 40 bots is fine, 40 bots × months of iterations is not | nit | Add the index |
| Destructive `acquire` then crash before `create_run` | Item taken, no run, no unique-index log | should-fix | Acquire inside the same IMMEDIATE tx as `INSERT runs`, or make acquire idempotent/nack on failure |
| No test for: DEFERRED vs IMMEDIATE deadlock; pause CAS vs `record_wake`; `wake_now` with existing `last_outcome`; rrule + `now` in the past; READY requeue vs `provider_gates` after a fresh process | These will pass CI and fail in production | blocks-shipping | Those five tests |

`ix_bots_due (status, next_due_at)` is fine at 40 bots. The O(n) trap is not that index; it is `due()` returning 50 blocked rows forever and `live_run_states()` if the partial unique index on `runs(bot_id)` is missing from an older file that this process still opens.

I have not executed this against `army/store.py` / `army/supervisor.py`. The READY/gate hole, the DEFERRED deadlock, and the `wake_now` bind are visible from the code you pasted. The inner-commit hole is the thing to prove with a test that `Store.transition(conn=)` does not commit.

## kimicli:kimi-code/k3

## Verdict up front

The design instincts are right (one-transaction succession, partial unique index, pure scheduler), but there are four real defects: a read-modify-write in `revise()` that both deadlocks under the two-process model and leaks a raw `IntegrityError`; a `READY`-requeue path that can spin a bot at full tick rate when the rate-limit artifacts are missing; a paused-*run* path that permanently unschedules the bot and false-alarms the stalled scan forever; and an rrule that runs out (or a pathological `FREQ=SECONDLY`) that the scheduler has no answer for. Plus one omission that will bite first in production: no visible `busy_timeout`/WAL discipline for the two-process SQLite file.

## 1. Concurrency

**`BotStore.revise()` is a deferred read-modify-write — upgrade deadlock + unmapped IntegrityError (blocks-shipping).** `revise()` runs `SELECT COALESCE(MAX(rev),0)` then `INSERT` then the CAS `UPDATE` inside `self._tx(conn)`. If `Store.atomic()` opens deferred transactions (your stated mix: BEGIN IMMEDIATE only for the multi-statement succession path), the interleaving is:

- Supervisor: `_transition` → `bots.atomic()` → `BEGIN IMMEDIATE` → holds RESERVED on the file.
- CLI: `revise` → deferred BEGIN → `SELECT MAX(rev)` → SHARED.
- Supervisor commits → wants EXCLUSIVE → waits on CLI's SHARED.
- CLI issues its `INSERT` → wants RESERVED → blocked by supervisor's RESERVED.

Neither can proceed; both fall out as `SQLITE_BUSY` once a timeout fires (or immediately, if no busy timeout is set — see §5). And even without the deadlock, two concurrent revises both read `MAX(rev)=N`, both insert rev N+1, and the second hits `UNIQUE(bot_id, rev)` — unlike `create()`, `revise()` does **not** translate `sqlite3.IntegrityError` into `ConcurrentTransition` (army/bots/store.py, `revise`), so a raw SQLite error reaches the CLI user. Fix: make `revise` (and anything that reads-then-writes) use `BEGIN IMMEDIATE`, and wrap the insert in the same IntegrityError→ConcurrentTransition translation `create()` already has.

**The `_tx()`/`nullcontext` mix has a concrete stale-object hazard (should-fix).** In `record_wake` and `set_status`, the in-memory `bot` is mutated (`bot.version += 1`, streaks, `next_due_at`) *after* the `with self._tx(conn)` block — unconditionally, even when `conn` joined an outer transaction that hasn't committed. In `BotSupervisor._transition`, if `set_status` (the `wake.exhausted` branch) raises `ConcurrentTransition` after `record_wake` succeeded, the whole `atomic()` block rolls back — but any caller-held `bot` object now claims a version and streaks the DB doesn't have. A subsequent CAS with that object fails with a misleading "moved on from version N" or, worse, a retry from a re-read hides that the first attempt's decision was lost. Fix: only apply the in-memory mutation when the method opened (and therefore committed) its own transaction, or return a fresh re-read.

**The mix is otherwise safe only if every multi-statement writer is IMMEDIATE.** Single-statement deferred writes are fine (SQLite autocommit is atomic per statement). Pure readers under deferred connections are fine for correctness but, combined with no busy timeout, turn contention into hard errors — see §5. The `_start` stranded-item race (live-check → `acquire()` → `create_run` loses to the CLI) is real and the code already confesses it in the log message: the work item *is* stranded, and "requeue it by hand" is not a recovery path, it's an admission. Either move `acquire()` after `create_run` with an explicit release-on-failure, or make the demo workload's `taken:` claim expirable. Should-fix; blocks-shipping if any real workload claims non-idempotent external resources.

## 2. Scheduler arithmetic

- **`RATE_LIMITED` returns `Wake(now, ...)` and trusts the vendor gate to exist (blocks-shipping).** The gate is only written if `run.artifacts["rate_limited_harness"]` is set (`_cool_vendor` returns silently otherwise), and the scan-side check `bot.harness is not None and bot.harness in blocked` skips gating entirely for a bot with `harness=None`. Either gap → the bot is due *now*, every tick, forever, hammering the vendor that just said stop — the exact quota spin the comments boast about preventing. Fix: floor the RATE_LIMITED wake at `now + RETRY_BASE_S` regardless of the gate, and log loudly when a RATE_LIMITED run carries no harness artifact.
- **Finite rrule that has run out (should-fix).** `next_occurrence` returns `None`; `_scheduled` wraps it as `Wake(None, WakeReason.SCHEDULE, ...)`. Result: ACTIVE bot, `next_due_at NULL`, `wake_reason=SCHEDULE` — which is *not* one of the two reasons `stalled()` excludes, so `fleet_tick` logs "succession was lost" every tick forever for a bot that simply finished its schedule, and nothing ever retires or pauses it. Fix: treat `None` from a finite rule as exhaustion — set status RETIRED (or PAUSED) in the record path, don't write a NULL/SCHEDULE wake.
- **Pathological valid rrule (should-fix).** `FREQ=SECONDLY` parses fine and is honored literally: the bot wakes every second, and the `MAX_IDLE_STREAK` cap is gated on `policy.kind is WakeKind.CONTINUOUS`, so an RRULE bot reporting NO_WORK wakes every second *forever* with an idle streak that grows unbounded and never pauses it. Fix: enforce a floor (`max(next_occurrence, now + min_interval_s)`) and apply the idle-streak cap to all kinds, not just continuous.
- **Unvalidated `policy.factor` (should-fix).** `_capped` with `factor <= 1` never reaches the ceiling, and with `factor <= 0` the retry branch computes `delay <= 0` → `next_due_at <= now` → a failing bot retries at full tick rate instead of the 30s base. The continuous branch has `max(delay, min_interval_s)`; the RETRY branch has no such floor. Fix: validate `factor > 1`, `jitter >= 0`, `base_s > 0` in `WakePolicy` construction, and floor the retry delay at `RETRY_BASE_S`.
- **`next_occurrence` doesn't guard `now` (nit/should-fix).** Only the `rrulestr` call is inside the `try`; `datetime.fromtimestamp(now, tz=UTC)` sits outside it. A caller passing milliseconds (`now` in ms is the classic federated-timestamp bug) or a pre-1970-negative-huge value raises `ValueError`/`OverflowError` uncaught, killing the tick. Fix: move `fromtimestamp` inside the guard or validate `now` at the API boundary.

## 3. State machine

**`PAUSED` in `_SETTLING` permanently unschedules the bot while the paused run still holds the live-run slot (blocks-shipping).** Sequence: human pauses a run mid-flight → `_transition(run, PAUSED)` → `classify` returns BLOCKED → `record_wake` writes `next_due_at=NULL, wake_reason=HUMAN`, streaks untouched. But `PAUSED` is *not* in the partial index's terminal set (`'continue','completed','failed'`), so the run is still "live": `due()`'s `NOT EXISTS` filters the bot out forever. If that run is never resumed, the bot is ACTIVE, unschedulable by time, unwakeable by `wake_now` (still has a live run), and unretirable (expiry is only checked in `_open_run`, which never sees it). `stalled()` *will* flag it — but see §4 on why that alarm won't survive contact with operators. Fix: pausing the last live run of a bot should either pause the bot (status, not just wake) or leave `next_due_at` alone; don't write a BLOCKED wake for a human-initiated park that nothing will ever answer.

**Succession fires twice for one logical iteration on the requeue path (should-fix, mostly benign).** READY is settling (RATE_LIMITED → wake at `now`), then the same run is re-dispatched and later reaches COMPLETED/FAILED — a second `record_wake` for the same iteration. Because the READY wake sets `next_due_at=now` while the run is still live, `due()` correctly suppresses a duplicate open, so the observable harm is limited to a misleading `last_outcome=rate_limited` being overwritten and a confusing ledger. The narrower real bug: if the process crashes after the READY succession but before re-dispatch, the bot sits due-but-blocked with a READY run held by nobody; on restart the base supervisor must re-arm it — worth a test, because if it doesn't, this is the lost-bot path.

**Default `WORK_DONE` biases toward hot loops (nit).** A COMPLETED/CONTINUE run whose workload forgot to declare `artifacts["outcome"]` resets both streaks and wakes a continuous bot at `min_interval_s` — fail-open toward maximum spend. Given the file's own doctrine ("continuous is not zero-delay scheduling"), the safe default for an undeclared completion is NO_WORK, or at minimum a warning metric when declaration is missing.

## 4. Escape hatches

- **`fleet_tick`'s stalled alarm false-alarms by construction, which guarantees real losses get tuned out (blocks-shipping operationally).** `stalled()`'s own docstring says the approvals-table filter is the *caller's* job — and `fleet_tick` doesn't do it. Every bot parked in WAITING_HUMAN with a resolved-or-orphaned approval, every run-paused bot (§3), and every finished finite-rrule bot (§2) logs ERROR every tick, unthrottled. Within a week the channel is noise, and the day a succession genuinely is lost, the signal is indistinguishable. You'd notice the *real* loss only by the absence of a bot's expected output, days later. Fix: do the approvals join in `stalled()`, throttle per-bot (log once per state change, not per tick), and include `run_id`/`next_due_at`/`updated_at` in the line.
- **`_retire_if_expired` only ever runs inside `_open_run`, so a blocked/expired bot is never retired (should-fix).** Expiry is checked only for bots the *due scan* returns. A bot with `next_due_at IS NULL` (waiting on a human) that passes `expires_at` stays ACTIVE forever; `stalled()` flags HUMAN-reason bots as candidates but `fleet_tick` only logs. Combined with the `suppress(ConcurrentTransition)` + `return True` (claims retired when the CAS lost), expiry is the least reliable lifecycle move in the file. Fix: check expiry in the `stalled`/fleet sweep too, and return `True` only when the write actually landed.
- **The swallows log at `debug` (nit).** `_back_off` and `_pause` swallow `ConcurrentTransition` at DEBUG — invisible at default INFO. The one place you *do* log at ERROR (unknown precondition, `_start` index refusal) is good; make the swallowed CAS losses at least WARNING-with-counters, because a silently un-backed-off continuous bot is a quota bill.

## 5. Omissions that will bite

- **No `busy_timeout` / WAL anywhere in evidence (blocks-shipping).** The entire two-process story (supervisor + CLI on one SQLite file) assumes lock waits resolve gracefully. Without `PRAGMA busy_timeout=N` and `PRAGMA journal_mode=WAL` in `Store`'s connection setup, every contention window — including the §1 deadlock — surfaces as an uncaught `sqlite3.OperationalError: database is locked` that kills the tick or errors the CLI. One-line fix each; add a test with two real connections.
- **No logging at the succession write itself (should-fix).** The single most important write in the system — `record_wake` deciding a bot's next wake — emits nothing. At 3am you get the *symptom* (stalled ERROR) with no `run_id`, no outcome, no computed `next_due_at`, and no which-transition-wrote-NULL. One `_logger.info` in `_transition`'s settling branch (bot, target, outcome, wake) pays for itself the first incident.
- **`children()` has no supporting index (nit).** `ix_bots_lineage` is on `(root_bot_id, depth)` but `children()` filters `parent_bot_id` — full scan per call. Fine at 40 bots; add the index when you add the retire cascade that will call it in a loop.
- **`revisions()` and `list()` are unbounded reads (nit).** Revisions grow forever per bot; cap or paginate before the ledger UI does.
- **Untested error paths (should-fix).** Concretely: `revise` concurrent-revision conflict, `_start`'s stranded-item branch, the nested-`conn` rollback semantics in §1, and `next_occurrence` returning `None`. These are exactly the paths whose behavior you currently know only from reading the code.
4/4 harnesses answered.

