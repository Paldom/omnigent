Asking 5 harnesses: claude:claude-fable-5@max, codex:gpt-5.6-sol@ultra, agy:gemini-3.7-flash-high, grokcli:grok-4.6@xhigh, kimicli:kimi-code/k3 …
2026-08-23 10:30:01,808 WARNING  [-] advise member agy:gemini-3.7-flash-high failed: agy failed (exit 1): Error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 12h57m49s.


## claude:claude-fable-5@max

Answering from the provided context (this is the omnigent fork, not researchkit; the Databricks routing hook is a false positive — ignored).

## 1. Package boundary: (c)'s layout, without the Store subclass

First, an honest correction: upstream churn threatens **none** of your three options, because `army/` doesn't exist upstream — additions-alongside never conflict. The only true upstream contact in Bot mode is the **UI section**. So: no React/Electron patch. Ship the UI as CLI subcommands first, and later a page served by army's own process over Tailscale. Zero upstream files, forever.

The real boundary question is fork-internal. Pick `army/bot/` as a subpackage, **composition over one shared Store**:

```
army/bot/__init__.py
army/bot/schema.py       # _BOT_SCHEMA string + migrate(conn)   (see Q3)
army/bot/model.py        # Bot dataclass, DRAFT/ACTIVE/PAUSED/RETIRED, derived_status() — pure
army/bot/schedule.py     # outcome → next_due_at, streak backoff — pure functions
army/bot/store.py        # bot queries; every one takes the core Store (and conn=)
army/bot/supervisor.py   # BotSupervisor (see Q4)
army/bot/cli.py          # `army bot …`
tests/army/bot/
```

The seam is two rules, one enforced by a test: (1) **nothing in `army/` core imports `army.bot`** — one test that greps core modules for the string, with a single sanctioned exception: `army/cli.py` lazy-imports `army.bot.cli` inside the `bot` subcommand function; (2) exactly one `Store`, one SQLite file — bot code extends it via Q2's `conn=` seam, never a second store.

Failure modes of the rejected two:

- **(a) flat growth**: you recreate upstream's `db_models.py` hotspot inside your own fork — 32 tickets all editing `store.py`/`state.py`, no import boundary, and within a quarter you can't tell core from bot or keep the 1278-line suite green while bot code churns.
- **(b) sibling `bots/`**: atomic succession must span `runs` + `commands` (army's tables) + `bots` in **one** SQLite transaction, and `runs` itself gains `bot_id`. A sibling package either opens a second DB file (no cross-file transactions) or reaches into `army.Store._connect` privates and ALTERs army's core table from outside. The boundary is theater, and you pay for it with a second config/CLI/entrypoint.
- **(c) as stated, Store subclass**: the base class's methods each open and commit their own connection, so a subclass can never join them into one transaction — you'd override `transition`/`consume` wholesale, forking the store inside the fork, plus two schema bootstraps racing in `__init__`.

## 2. Store: optional-connection passthrough, ~25 lines

Don't rewrite 522 lines. Add one context manager and thread an optional `conn` through only the methods succession touches (3–5 of them). Default `None` = today's behavior, so all 1278 test lines pass untouched.

```python
# army/store.py — additions only
@contextmanager
def transaction(self) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(self._path, isolation_level=None)  # no DBAPI magic
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("BEGIN IMMEDIATE")   # take the write lock now, not on upgrade
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

@contextmanager
def _connect(self, conn: sqlite3.Connection | None = None):
    if conn is not None:
        yield conn          # caller owns BEGIN/COMMIT
        return
    # ... existing open/commit/close body, unchanged ...

def transition(self, run_id, src, dst, *, version, conn: sqlite3.Connection | None = None): ...
def consume_command(self, command_id, *, conn: sqlite3.Connection | None = None): ...
```

Succession then reads exactly like the design:

```python
with store.transaction() as conn:
    store.transition(run_id, EVALUATING, COMPLETED, version=v, conn=conn)
    store.consume_command(cmd.id, conn=conn)
    bot_store.set_next_due(store, bot_id, due, expected_version=bv, conn=conn)
```

`ConcurrentTransition` raised inside rolls back all three — which is precisely the replay-is-a-no-op guarantee. Two non-negotiables: `isolation_level=None` + explicit `BEGIN IMMEDIATE` (Python's implicit-transaction rules would otherwise leave your CAS `SELECT` outside the transaction, and DEFERRED begins upgrade-deadlock under two writers), and see Q6 for what must *not* go inside the `with`.

## 3. Migration: column detection, not `user_version`

`user_version` is a second source of truth, and it lies exactly in your situation — a fast-moving fork where a downgrade (rebase rollback) then re-upgrade can cross branches whose migration lists differ. Interrogate the actual database instead; it can't drift from itself:

```python
# army/bot/schema.py — run inside one store.transaction() at BotSupervisor startup,
# right after core _SCHEMA. ponytail: linear column checks, not a framework;
# becomes a framework only if a migration ever needs data rewriting.
def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    for col in ("bot_id", "revision_id", "outcome"):
        if col not in cols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS one_live_run_per_bot
        ON runs(bot_id)
        WHERE bot_id IS NOT NULL
          AND state NOT IN ('continue','completed','failed')""")
    conn.executescript(_BOT_SCHEMA)   # CREATE TABLE IF NOT EXISTS × 8
```

Idempotent (every statement is a checked or `IF NOT EXISTS` op). Downgrade-safe (old code's `CREATE TABLE IF NOT EXISTS` no-ops; added columns are nullable so old INSERTs still work; NULL `bot_id` rows never trip the partial index) — **with one precondition you must verify before committing to `ADD COLUMN`**: that `store.py` selects named columns, not `SELECT *` with positional unpacking. If it uses `SELECT *`, extra columns break *downgraded* code — fix those queries first, as part of this ticket.

## 4. Supervisor: (c) composition, plus one surgical extraction in core

The honest model: `tick()` today does two jobs — run-level advancement and workload-pull intake. Bots replace **intake** (clock/insert-woken, bot-level query) and reuse **advancement** (run-level, unchanged). So make one default-compatible change to core:

```python
# army/supervisor.py — only change: extract per-run advance, optional workload
def tick(self):
    for run in self.store.active_runs(workflow=self.workload.name):
        self.advance(run)
    self._maybe_open_run()                      # intake, unchanged

def advance(self, run, workload: Workload | None = None):
    wl = workload or self.workload
    # ... existing per-run body, verbatim ...
```

Existing single-workload tests stay green (`tick` behavior is identical). Then:

```python
# army/bot/supervisor.py
class BotSupervisor:
    def __init__(self, store, omni, registry: dict[str, Workload], lanes):
        self._sups = {name: Supervisor(store, omni, wl, lanes=lanes)  # SHARED lanes object
                      for name, wl in registry.items()}
    def tick(self, now):
        for bot in due_bots(self._store, now):        # ONE indexed query: next_due_at <= now, ACTIVE
            if scripted_precondition(bot):            # LLM-free, mechanic #5
                spawn(self._store, bot)               # INSERT run; IntegrityError on the
                                                      # partial index == already live, skip
        for run in active_bot_runs(self._store):      # run-level loop, as today
            self._sups[workload_of(run)].advance(run) # intake never called on these
```

Reject (a): N independent tick loops turn the wake scan into N polls of the `bots` table, and per-supervisor lanes silently break global per-vendor caps — and bots don't `acquire()`, clocks and inserts wake them, so you'd be faking pull semantics over push scheduling. Reject (b): stuffing a registry into core `Supervisor` churns the tested class yet still misrepresents the scheduling — resolution isn't the change; the intake *source* is. On cadence: both phases live in **one loop in one process** — a bot-level query then a run-level loop. Two queries, not two daemons.

## 5. The slice: "a bot wakes on a clock and you can watch it"

Build in this order — each step lands with its tests, and steps 1–2 are the foundation everything else sits on:

1. Q2's `transaction()` + `conn=` seam (spawn already needs it: run INSERT + `next_due_at` CAS update, one transaction).
2. Q3's migration + `bots`/`bot_revisions` tables.
3. `schedule.py` + `model.derived_status()` — pure functions; the property tests here are cheap and load-bearing.
4. `BotSupervisor` spawn+advance wiring a **keyless** existing workload (your hackernews-class providers cost nothing).
5. CLI: `army bot create/status/wake/pause/retire`.

Day-one human path: `army bot create demo.toml` → bot-supervisor loop running → watch `army bot status` derive `SCHEDULED → DUE → RUNNING → SCHEDULED`, `next_due_at` computed from `NO_WORK`/`WORK_DONE`, `idle_streak` climbing. That exercises every load-bearing mechanic (migration, transaction, partial index, outcome policy, derived status) and none of the product surface.

**Honest stubs, each with a test that documents the ceiling:** `usage_ledger` records every run append-only but enforces nothing (`test_ledger_records_but_never_blocks`) — record from day one because usage history can't be backfilled; `BLOCKED` sets `next_due_at = NULL` and only manual `army bot wake` revives it (`test_blocked_requires_manual_wake`) — the CLI wake is 5 lines and stays useful after the bus exists. **Not written at all:** `messages`/`message_deliveries`, approvals/Broker wiring (it's never constructed even in core today — reuse the existing WAITING_HUMAN + approve/deny path meanwhile), `bot_docs`, `provider_gates`, budget enforcement, succession/lineage, UI. Each arrives with its ticket; half-built versions of these are debt, not progress.

## 6. The mistake you'll make: holding the new transaction across an HTTP call

Q2 hands you a hammer, and within the first 2000 lines you will wrap `advance()` — which calls Omnigent over HTTP during dispatch/collect — in `store.transaction()` "for atomicity." It passes every test (fakes are instant, single process). The symptom in production: intermittent `sqlite3.OperationalError: database is locked` from `army status` and the CLI *only while a body is mid-dispatch*; you'll add `busy_timeout` and convert the error into mysterious 30-second CLI hangs; then one crash mid-dispatch produces a duplicate successor, because your replay-no-op key protects the DB statements but the HTTP call inside the transaction happened anyway and can't be rolled back.

The rule that prevents it: **a transaction contains only reads and writes of values already in memory — never I/O**. That's what your `effects`/`commands` tables are for: write intent, commit, do the network call, write the outcome in a *new* transaction. Atomic succession qualifies for `transaction()` precisely because all three statements are pure state writes. Put that sentence as a comment on `transaction()` itself; it's the 3am page in this design.

---
One housekeeping note: the ponytail plugin wants a statusline badge configured in `~/.claude/settings.json` — say the word and I'll add it.

## codex:gpt-5.6-sol@ultra

Use **(b): a sibling `bots/` package**, backed by the same SQLite database, plus two small generic seams in `army`: transaction sharing and workload resolution. Keep scheduling outside `Supervisor`.

1. **Package boundary.**

```text
bots/
  models.py
  schema.py
  repository.py
  registry.py
  scheduler.py
  messages.py
  service.py
  runtime.py
  api.py
  cli.py
tests/bots/
web/src/bots/
```

`bots → army`, never the reverse. `BotRepository` composes `Store`; it does not inherit it. `army` owns runs, transitions and workload execution; `bots` owns bot lifecycle, revisions, scheduling, derived statuses, messages and policy.

Reject (a): bot-specific branches will spread through `army/store.py`, `supervisor.py` and `cli.py`, making every bot ticket risk the existing control plane. Reject (c): inherited `Store` methods still open independent connections, so the subclass appears transactional while silently committing separately.

An integrated UI cannot literally be conflict-free: upstream hard-codes routes in [`web/src/App.tsx`](https://github.com/omnigent-ai/omnigent/blob/main/web/src/App.tsx) and navigation in [`web/src/shell/Sidebar.tsx`](https://github.com/omnigent-ai/omnigent/blob/main/web/src/shell/Sidebar.tsx). Keep all implementation in `web/src/bots/` and accept two tiny carried patches. Absolute zero-conflict requires a separately served Bot SPA.

2. **Store transaction seam.** The current [`Store.transition`](https://github.com/Paldom/omnigent/blob/agent-army/army/store.py) already consumes an optional command atomically. Expose its connection and add one optional keyword:

```python
@contextmanager
def transaction(
    self, *, immediate: bool = False
) -> Iterator[sqlite3.Connection]: ...

def transition(
    self,
    run: Run,
    target: RunState,
    *,
    artifacts: dict[str, Any] | None = None,
    outstanding: list[str] | None = None,
    approval_id: str | None = None,
    terminal_reason: str | None = None,
    attempt: int | None = None,
    consume: Command | None = None,
    now: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> Run: ...
```

Move the existing SQL body into `_transition(conn, ...)`. With no `conn`, preserve today’s open/commit/close behavior. A supplied connection is never committed or closed.

```python
# bots/repository.py
with store.transaction(immediate=True) as conn:
    if self._run_has_outcome(predecessor.id, conn):
        return False  # replay

    store.transition(
        predecessor, terminal_state,
        consume=command, now=now, conn=conn,
    )
    self._set_run_outcome(predecessor.id, outcome, conn)
    self._update_successor_due(
        predecessor_run_id=predecessor.id,
        expected_bot_version=bot.version,
        next_due_at=next_due_at,
        conn=conn,
    )
```

The bot update resolves `bot_id` through the predecessor run, CASes `bots.version`, and requires `rowcount == 1`; otherwise the whole transaction rolls back.

3. **Schema migration: use column detection**, not `user_version`, for this one additive upgrade.

```python
# bots/schema.py
with store.transaction(immediate=True) as conn:
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(runs)")
    }
    for name, ddl in {
        "bot_id": "TEXT",
        "revision_id": "TEXT",
        "outcome": "TEXT",
    }.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_one_live_bot
        ON runs(bot_id)
        WHERE state NOT IN ('continue', 'completed', 'failed')
    """)
```

Create the new tables similarly with fixed `CREATE TABLE IF NOT EXISTS` statements. Use individual `execute()` calls—`executescript()` can break the explicit transaction boundary. Nullable additions and SQLite’s multiple-`NULL` unique-index behavior make old binaries structurally compatible; do not remove columns on downgrade. Operationally, stop scheduling and drain bot runs before downgrading, because an old supervisor cannot maintain `outcome/next_due_at`.

4. **Supervisor: choose (b), with backward compatibility.**

```python
class WorkloadRegistry(Protocol):
    def require(self, name: str) -> Workload: ...

class Supervisor:
    def __init__(
        self,
        store: Store,
        omni: OmniClient,
        workload: Workload | None = None,
        *,
        workloads: WorkloadRegistry | None = None,
        lanes: Lanes | None = None,
        max_concurrent_runs: int = 3,
    ) -> None: ...

    def advance_runs(
        self, runs: Iterable[Run], *, now: int | None = None
    ) -> TickReport: ...
```

Wrap the legacy `workload` in a singleton registry, so existing construction and `tick()` tests remain unchanged. `advance_runs()` resolves `registry.require(run.workflow)` and passes that workload through `_advance`, `_dispatch`, `_collect`, `_ask`, and `_apply_command`.

`bots/runtime.py:BotLoop` owns two clocks: frequent run advancement and a separate due-bot scan. It calls `Supervisor.advance_runs(...)`; the scheduler creates at most the available number of READY runs. N Supervisors would turn the global concurrency cap into N independent caps. An unmodified composed `BotSupervisor` would either repeat scans or hide routing inside a fake workload.

5. **Day-one vertical slice.** Ship one fixed-interval/manual bot using one real configured workload:

```text
create bot → activate immutable revision → run-now/due scan
→ READY run pinned to revision/workflow → real Omnigent execution
→ existing human command if required → outcome → atomic next_due_at
→ CLI status
```

Expose it through `python -m bots.cli create|activate|run-now|tick|status`. Use real SQLite, scheduler and Omnigent path; fake only the clock, `OmniClient`, and workloads in tests.

Until implemented, activation must explicitly reject `continuous` and event/message wake policies, with tests documenting `FeatureUnavailable`; never substitute an always-true precondition. Do not yet write placeholder UI, message tables, docs, budgets, provider gates or owner approvals. In particular, reject privileged actions rather than faking `Broker` authorization.

6. **The likely mistake:** deriving the unique-index predicate from `army.state.TERMINAL`. That set currently includes `PAUSED` ([`army/state.py`](https://github.com/Paldom/omnigent/blob/agent-army/army/state.py)). The resulting index would exclude paused runs, allowing the scheduler to create a second run for that bot. When the owner resumes the paused run, two bodies advance and duplicate messages or external effects. Hard-code and test the exact three-state exclusion.

## agy:gemini-3.7-flash-high

*failed: agy failed (exit 1): Error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 12h57m49s.
*

## grokcli:grok-4.6@xhigh

I'll ground the six answers in the actual `army/` code so the seams, signatures, and first slice match the repo rather than the design notes.The workspace isn’t the fork; I’ll hunt down `army/` on disk so the file paths and signatures match what’s actually there.The six answers below assume `army/` is already fork-owned (rebase-free). I have not read the live `store.py` in this session; method names match the context you gave. If `transition` is actually `cas_transition`, keep the shape and rename.**Put Bot mode in `army/bot/` as a subpackage that imports `army` core and is never imported by it. One `Store`. No Store subclass. `transaction()` is the only real core splice; `advance_runs()` is the only Supervisor splice. Column detection, not `user_version`. `BotSupervisor` composes N single-workload Supervisors and is the only thing allowed to spawn. Day-one slice: one scripted bot, CLI, atomic succession. The bug you will hit is calling `Supervisor.tick()` for spawn.**

---

### 1. PACKAGE BOUNDARY — `army/bot/`, same Store, one-way import

Not (a), not (b), not (c) as written. Layout of (c), Store of (a).

`army/` is already the rebase-free zone. Upstream will not fight you there. The fight is *you vs you* on `army/store.py` / `army/supervisor.py` if every bot table and the scheduler land in the flat package.

```
army/store.py              # splice only: transaction() + _migrate() for runs columns/index
army/supervisor.py         # splice only: extract advance_runs() from tick()
army/cli.py                # splice only: subparser that calls army.bot.cli.main
army/bot/__init__.py       # empty of imports — do not re-export into army
army/bot/schema.py         # BOT_SCHEMA + install_bot_schema(conn)
army/bot/persist.py        # bots/revisions/messages SQL, all via store.transaction()
army/bot/succession.py     # the one atomic write
army/bot/schedule.py       # outcome → next_due_at
army/bot/status.py         # derived 9-way status, read-time only
army/bot/supervisor.py     # BotSupervisor
army/bot/cli.py
army/bot/workloads/noop.py
tests/army/bot/
```

UI: new files only (whatever tree the Electron app uses — a new route/section directory). The one allowed carried patch is a single nav link. `omnigent/db/db_models.py` stays untouched; bot tables are sqlite in `army`, not Omnigent's ORM.

**Seam:** `army.bot` imports `Store`, `Supervisor`, `Workload`, `RunState`. Nothing in `army/*.py` except the three splices above imports `army.bot`. Bot schema is installed by the bot entrypoint and the bot pytest fixture, not by `Store.__init__`. Existing `tests/army/` never see bot tables.

**(a) fails as** `army/bots.py` + `army/messages.py` + 8 tables of methods on `Store`. `store.py` becomes the file every army ticket and every bot ticket touch. You will rebase against yourself weekly, and `tests/army/conftest.py` will grow bot fixtures that leak into single-workload tests.

**(b) fails as** atomic succession. `_connect()` is private and per-method. A sibling `bots/` that cannot be imported by `army` cannot share a sqlite transaction with `runs`/`commands`. You will call three Store methods in a row, comment “TODO txn”, then the partial unique index will start raising `IntegrityError` under concurrent ticks — and you will catch it, which hides lost `next_due_at` writes. The Supervisor registry also lives in `army`; “never imported BY army” just forces a dishonest patch into `supervisor.py` anyway.

**(c)’s Store subclass fails as** two `_connect()`s, two `_SCHEMA`s, two connections. A subclass that opens its own connection cannot join the parent’s transaction, so succession is still three commits. Mixin-on-same-instance is the same Store with extra methods; that is (a) wearing a class hierarchy.

---

### 2. THE STORE PROBLEM — join the existing connection, do not rewrite methods

Add one public context manager. Change `_connect` so it yields the in-flight connection and **does not commit**. Every existing method keeps its signature. The 1278 lines keep auto-commit-per-call.

```python
# army/store.py

@contextmanager
def transaction(self) -> Iterator["Store"]:
    """One sqlite writer txn for nested Store calls. Reentrant no-op."""
    if self._txn_conn is not None:
        yield self
        return
    conn = sqlite3.connect(self._path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.isolation_level = None          # we emit BEGIN ourselves
    conn.execute("BEGIN IMMEDIATE")
    self._txn_conn = conn
    try:
        yield self
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        self._txn_conn = None
        conn.close()
```

`_connect` grows a 4-line prelude only:

```python
@contextmanager
def _connect(self):
    if (conn := self._txn_conn) is not None:
        yield conn
        return
    # existing open / yield / commit / rollback / close unchanged
```

Do **not** make every `_connect` implicitly nest (that changes “inner method already committed if outer then fails”). Only `transaction()` opts in.

Succession does **not** become a 522-line rewrite and does **not** live on `Store`:

```python
# army/bot/succession.py
@dataclass(frozen=True)
class Succession:
    run_id: str
    expected_run_version: int
    to_state: RunState          # CONTINUE | COMPLETED | FAILED
    command_id: str
    bot_id: str
    expected_bot_version: int
    outcome: str                # WORK_DONE | NO_WORK | ...
    next_due_at: datetime | None

def succeed(store: Store, spec: Succession) -> None:
    with store.transaction():
        store.transition(spec.run_id, spec.to_state, spec.expected_run_version)
        store.consume_command(spec.command_id, spec.run_id)   # existing name
        set_bot_due(store, spec.bot_id, spec.expected_bot_version,
                    spec.next_due_at, spec.outcome)
```

`set_bot_due` lives in `army/bot/persist.py` and uses `_connect` via one escape hatch so you do not teach `Store` the `bots` table:

```python
# army/store.py — the other allowed splice
def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    with self._connect() as conn:
        return conn.execute(sql, params)
```

`BEGIN IMMEDIATE` is load-bearing: the unique index is a writer lock; DEFERRED lets two ticks both not-see a live run.

---

### 3. SCHEMA MIGRATION — `PRAGMA table_info`, not `user_version`

`user_version` becomes a framework the moment you have 3 upgrades and a skipped integer. A downgrade that leaves `user_version` high will skip a migration you later insert in the gap; a downgrade that writes it backward is a data lie. You have additive columns and new tables. Detect objects.

```python
# army/store.py — called from __init__ after _SCHEMA
def _migrate(self, conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    for name, decl in (
        ("bot_id", "TEXT"),
        ("revision_id", "TEXT"),
        ("outcome", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS runs_one_live_per_bot
        ON runs(bot_id)
        WHERE bot_id IS NOT NULL
          AND state NOT IN ('continue', 'completed', 'failed')
        """
    )
```

Rules that keep this from becoming a framework:

- Every `ADD COLUMN` is nullable (or has a `DEFAULT`). Old army `INSERT` lists keep working. Downgrade cannot corrupt: extra columns are ignored; the index stays; duplicate NULL `bot_id`s are allowed by SQLite.
- Never `DROP COLUMN`. Never rewrite rows. Never bump a version integer.
- New tables do **not** go in `_SCHEMA`. They go in `army/bot/schema.py` as `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`, applied by `install_bot_schema(conn)` from `army.bot` startup and `tests/army/bot/conftest.py`. Army tests on a 4-table DB stay 4-table.
- Idempotent on a fresh DB, on a live army DB, and on a DB that already ran bot mode.

---

### 4. THE SUPERVISOR REFACTOR — (c) compose, extract `advance_runs()`, never (b)

(b) is the one that will stay green and still be wrong. A `dict[str, Workload]` on `Supervisor` makes `self.workload.acquire()` meaningless and forces `active_runs()` to stop filtering by `self.workload.name`. Single-workload tests keep passing through the compat shim; the registry path is an untested branch; two bots on the same workload will step each other.

(a) as the *outer* loop is the same bug with more objects: people will write `for wl in workloads: Supervisor(..., wl).tick()` and `tick()` still `acquire()`s.

Keep `Supervisor.__init__(store, omni, workload, ...)` exactly. Existing tests construct that and call `tick()`. Split the body:

```python
# army/supervisor.py
def tick(self) -> None:
    self.advance_runs()
    self._maybe_acquire()

def advance_runs(self) -> None:
    for run in self.store.active_runs(workflow=self.workload.name):
        self._step(run)   # at most one state, unchanged
```

That is the whole core change. `tick()` still acquire()s one run per call; tests stay honest.

```python
# army/bot/supervisor.py
class BotSupervisor:
    def __init__(self, store: Store, omni, workloads: dict[str, Workload], **kw):
        self.store = store
        self.workloads = workloads
        self._by_wl = {
            name: Supervisor(store, omni, wl, **kw)
            for name, wl in workloads.items()
        }

    def tick(self, now: datetime) -> None:
        self._wake(now)                    # bot cadence
        for sup in self._by_wl.values():
            sup.advance_runs()             # run cadence; no acquire

    def _wake(self, now: datetime) -> None:
        for bot in due_bots(self.store, now):          # next_due_at <= now AND IS NOT NULL
            if bot.live_run_id:
                continue
            rev = bot.pinned_revision
            wl = self.workloads[rev.workload_name]
            if not wl.precondition(bot, rev):          # LLM-free, required for continuous
                bump_idle(self.store, bot)
                continue
            spawn_run(self.store, bot, rev)            # INSERT runs, CAS bot version
```

Wake and advance may share a process loop; they must not share a method. `_wake` is the only spawner. `Workload.acquire` is not on this path.

Do not call `sup.tick()` from `_wake`. That is §6.

---

### 5. WHAT TO BUILD FIRST — one scripted bot you can `run --once`

Human day-one path:

```
python -m army.bot create --name ping --every 60s --workload army.bot.workloads.noop:NoopWorkload
python -m army.bot run --once
python -m army.bot status
```

`NoopWorkload` implements `dispatch/collect/evaluate/apply` as no-ops and reports `WORK_DONE`. Precondition: `return True`. No Omnigent call, no API key.

**Write for that path (and tests that can fail):**

| Piece | Why it is not optional |
|---|---|
| `transaction()` + `_migrate` columns/index | succession and live DBs |
| `bots` + `bot_revisions` | a Bot is rows; runs pin `revision_id` |
| `succeed()` in one txn | the load-bearing mechanic |
| `outcome` → `next_due_at` for `WORK_DONE` / `NO_WORK` | or it is not a loop |
| `BotSupervisor._wake` + `advance_runs` | or you will go through `acquire()` |
| derived status projector | 40 lines; if you skip it you will store the 9 statuses |
| CLI `create / run --once / status` | the human exercise |
| `tests/army/bot/test_succession.py` | crash between the three writes, assert replay is no-op |
| `tests/army/bot/test_one_live_run.py` | second INSERT raises; `paused` still in the index |

**Honest stubs (a test that names the ticket and asserts the limitation):**

- `Budget.charge()` → `return Allow()`. `test_budget_charge_does_not_persist` (you may overspend).
- `inbox(bot_id)` → `[]`. `test_messages_are_not_a_bus_yet`.
- `RATE_LIMITED` / `BLOCKED` / `RETRYABLE_ERROR` schedule functions exist and are unit-tested; `_wake` treats `next_due_at IS NULL` as *not due* (`test_blocked_is_not_in_due_scan`). No wake-on-insert yet.

**Do not write at all:** UI, `bot_docs`, `approval_requests`, `provider_gates`, `usage_ledger` table, `Broker` wiring, DRAFT→ACTIVE publish, bot↔bot routing, `ALWAYS_OWNER` owner channel. An `xfail` list in `tests/army/bot/test_backlog.py` is the backlog, not empty files.

---

### 6. THE ONE THING — `BotSupervisor.tick()` will call `Supervisor.tick()`

You will write, in the first 2000 lines, something that looks like reuse:

```python
for bot in due_bots(...):
    self._by_wl[bot.workload_name].tick()
```

Single-bot tests pass: one `acquire()`, one live run, unique index quiet.

**Symptom when you add a second bot with the same workload:** `acquire()` fires once per Supervisor per outer tick, not once per due bot. Either bot B starves, or both Supervisors (if you built one per bot instead of per workload name) both see `active_runs(workflow=X) == []`, both `acquire()`, one `INSERT` hits `runs_one_live_per_bot`, and you `except sqlite3.IntegrityError: pass`. That catch is the silent one: `acquire()` already consumed whatever the Workload uses as a queue, the rejected body is gone, `next_due_at` is stale, bot B looks “scheduled” in the derived status and never runs.

You will then “fix” it with `max_concurrent_runs = n_bots` or one `Supervisor` per bot. That papers over `acquire()` until a `PAUSED` run (still in the unique index, still in `active_runs`) consumes a slot and a `DUE` sibling cannot spawn.

The fix is the one in §4: `advance_runs()` only, spawn only in `_wake`, never `tick()`, never `acquire()`, never `except IntegrityError`. If the unique index fires, that is a bug in `_wake`’s live-run check, not a control-plane signal.

## kimicli:kimi-code/k3

## 1. Package boundary: (b) sibling top-level package — but named `army/bots/`, not `bots/`

Actually, a refinement of (b): make it `army/bots/` — a subpackage that imports from `army` but is **never imported by any existing `army/` module**. This keeps it inside your "additions alongside" class per `FORK-DELTA.md` (zero rebase cost, by your own accounting) while keeping the dependency arrow strictly one-directional: `army/bots/*` → `army/*`, never the reverse.

The seam is a file you write once and defend: `army/bots/__init__.py` re-exports nothing from `army`, and `army/__init__.py` (or `army/cli.py` entry) gets exactly one new line — a lazy import guarded so that deleting `army/bots/` leaves upstream-mergeable `army/` untouched. Something like `army/bots/{store,supervisor,scheduler,bus,workloads,ui}/` as submodules, not flat `bots.py` files.

- Rejected (a) in-place growth: every upstream PR that touches `army/store.py` or `army/supervisor.py` — and with 100 issues/week, some will, the moment upstream absorbs your six carried patches or refactors the control plane — conflicts with your grown files. You'd convert "additions alongside" into a seventh, eighth, ninth carried patch.
- Rejected (c) Store subclass: subclassing couples you to `Store`'s private `_connect()` internals. When upstream renames that or changes connection handling, your subclass breaks at **runtime**, not at merge time — the worst failure mode for a single-box deployment. Compose, don't inherit.

## 2. The Store problem: add an explicit transaction handle, keep every existing method

Don't touch the 522 lines. Add one public method and one private convention:

```python
class Store:
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One connection, one commit. For multi-statement atomic units."""
        conn = self._connect()          # reuse existing connect logic, minus auto-commit
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
```

Then the existing methods stay exactly as they are (each still self-transactional, all 1278 test lines pass unchanged), and new bot-mode code writes its atomic units against the raw connection inside `transaction()`:

```python
with store.transaction() as conn:
    # terminal transition, keyed on predecessor id — replay is a no-op
    cur = conn.execute(
        "UPDATE runs SET state=?, outcome=?, version=version+1 "
        "WHERE id=? AND version=? AND state NOT IN ('completed','failed')",
        (terminal, outcome, run_id, expected_version))
    if cur.rowcount == 0:
        return  # already done — idempotent replay
    conn.execute("UPDATE commands SET consumed_at=? WHERE id=? AND consumed_at IS NULL", ...)
    conn.execute("UPDATE bots SET next_due_at=? WHERE id=?", ...)
```

The signature you're adding is `Store.transaction() -> Iterator[sqlite3.Connection]` — nothing else. Do **not** refactor existing methods to accept an optional `conn=` parameter; that's the "minimal" change that quietly doubles the test surface. New atomic units live in `army/bots/store.py` as free functions taking `(store, ...)` and using `store.transaction()`. Old methods untouched, new code explicit, no subclass.

One caveat to verify: `_connect()` currently commits on exit, so `transaction()` must bypass or parameterize that contextmanager rather than nesting inside it — otherwise your "one transaction" silently becomes several.

## 3. Migration: `user_version` pragma — two of them

Use `PRAGMA user_version`. Column-detection (`PRAGMA table_info` sniffing) is the thing that doesn't survive downgrade: after a downgrade the columns still exist, detection says "migrated," and the old code is fine but your version bookkeeping is a lie. `user_version` is honest, idempotent by construction, and it's ~20 lines, not a framework:

```python
# army/bots/migrate.py
_MIGRATIONS: dict[int, str] = {
    1: """
    ALTER TABLE runs ADD COLUMN bot_id TEXT;
    ALTER TABLE runs ADD COLUMN revision_id TEXT;
    ALTER TABLE runs ADD COLUMN outcome TEXT;
    CREATE UNIQUE INDEX IF NOT EXISTS one_live_run_per_bot
        ON runs(bot_id) WHERE state NOT IN ('continue','completed','failed');
    """,
    # bots, bot_revisions, messages, ... — CREATE TABLE IF NOT EXISTS
}

def migrate(conn: sqlite3.Connection) -> None:
    (v,) = conn.execute("PRAGMA user_version").fetchone()
    for target in sorted(_MIGRATIONS):
        if target <= v:
            continue
        conn.executescript(_MIGRATIONS[target])
        conn.execute(f"PRAGMA user_version = {target}")
```

Two specifics that bite people here:

- **Use two version counters, not one.** `army/store.py` owns `user_version` for the base schema (starts at whatever you stamp today); bot mode stamps its own somewhere it owns — e.g. a `_bot_schema_version` row in its own `bots` DB file, or if sharing the file, a single-row `schema_meta` table. If you share `user_version` with upstream-touched `store.py`, an upstream schema PR that also adopts `user_version` (plausible, it's the obvious fix to their non-migration problem) collides with your numbering. Your rebase note says `db_models.py` conflicts first — schema versioning is exactly where that fight replays inside `army/`.
- **Downgrade safety comes from ADD COLUMN, not from rollback.** Never write a `down` migration. SQLite `ADD COLUMN` with a nullable/default column is harmless to old code (it never selects the new columns), and the partial unique index is droppable without data loss. The rule is: migrations only *add*. If a future change needs a destructive reshape, that's a new-table-and-copy migration, versioned like the rest.

## 4. Supervisor: (c) compose — `BotSupervisor` wraps `Supervisor`, one per workload

Take (c), and be honest about why (b) is tempting but wrong: making `Supervisor` take a registry edits `__init__` and `tick()` — the two places every existing single-workload test touches — and bakes the two-cadence confusion into one class. (a) — N raw Supervisors driven by your outer loop — is nearly right but leaves the wake-scan logic homeless.

The shape:

```python
# army/bots/supervisor.py
class BotSupervisor:
    def __init__(self, store, omni, registry: dict[str, Workload], ...):
        self._store = store
        self._registry = registry
        self._inner: dict[str, Supervisor] = {}   # lazily built, one per workload name

    def tick(self, now: datetime) -> None:
        # CADENCE 1 (bot-level, cheap): one query over bots.next_due_at
        for bot in due_bots(self._store, now):       # SELECT ... WHERE next_due_at <= ? 
            if not check_precondition(bot):          # LLM-free scripted gate
                continue
            sup = self._inner.setdefault(
                bot.workload_name,
                Supervisor(self._store, self._omni,
                           self._registry[bot.workload_name], ...))
            sup.tick()                                # opens at most one run
        
        # CADENCE 2 (run-level): advance every live run one state,
        # independent of whether its bot was due this tick
        for sup in self._inner.values():
            sup.advance_active_runs()
```

Existing tests stay green because `Supervisor`'s constructor and `tick()` are untouched. The cadence split is made structural: the wake scan is a single indexed SQL query over `bots` (runs once per outer tick), run advancement iterates already-live runs (can run more often, or on event). If that split means `Supervisor.tick()` needs to be decomposed into `open_one()` + `advance()` — and it probably does — that's the *one* justified edit to `army/supervisor.py`: extract two methods from the body of `tick()`, keep `tick()` calling both. Pure refactor, behavior-identical, tests don't move. That's a small carried patch; file the upstream PR.

## 5. Vertical slice: one bot, one workload, CLI only, outcome → next_due_at

Day-one runnable path, in build order:

1. `migrate()` + `bots` table + `runs.bot_id/revision_id/outcome` (question 3). Nothing else of the schema.
2. `Store.transaction()` + the atomic-succession function (question 2). This is the load-bearing mechanic; everything else is scheduling sugar.
3. One hardcoded continuous bot in a TOML seed, one trivial scripted workload (e.g. "check a mailbox folder, summarize unread count" — LLM-free precondition = "folder non-empty").
4. `BotSupervisor.tick()` + the outcome→`next_due_at` computation for exactly three outcomes: `WORK_DONE` (fixed interval), `NO_WORK` (backoff), `BLOCKED` (NULL). 
5. CLI: `bots list / bots status / bots pause / bots wake <id>` — argparse, mirroring `army/cli.py`.

A human can then: create the bot, watch it wake, run, report `NO_WORK`, back off, get woken by an insert. That's the whole idea — "a bot is data, a body is disposable" — exercised end to end.

**Honest stubs with documenting tests** (the stub is the test's subject — `test_messages_stub_raises_not_implemented_and_documents_shape`):

- `messages`/`message_deliveries` — stub the `send()`/`ack()` interface; the test asserts the ack-after-commit contract in docstring form. Bot↔bot is not in the slice.
- Derived status — implement the function, but only the 4 of 9 statuses the slice can produce; test asserts the other five return `SCHEDULED` as the documented fallback.
- `budget_accounts`/`usage_ledger` — a no-op ledger that records rows but never blocks. Test: "budget exhaustion does not yet gate dispatch — ticket N."

**Not written at all** (a stub here is a lie, not a placeholder): `approval_requests`/`Broker` wiring (security-critical — a stubbed approval gate is worse than none; `Broker` is famously never constructed today, so there's no behavior to preserve), the UI section, `provider_gates`, `bot_revisions` pinning beyond storing `revision_id` as a plain field.

## 6. The one thing you'll get wrong

**You'll let the atomic-succession transaction leak, and the partial unique index will start throwing `IntegrityError` at 3am under exactly the replay path you designed it to survive.**

Concrete symptom: a tick crashes (or the Mac sleeps) after the run's terminal transition commits but before `next_due_at` is written — because somewhere in the succession path, one of the three writes went through an *existing* `Store` method (`store.transition(...)` — which opens its own connection and commits immediately) instead of through the shared `transaction()` connection. Now the bot has `next_due_at` stale or NULL, a later wake or human nudge tries to open a successor run, and `INSERT INTO runs (bot_id, state...) VALUES (..., 'ready')` hits `one_live_run_per_bot` — because a row in `dispatching` from the interrupted successor attempt is still inside the index. The replay-is-a-no-op logic never runs, because the replay check keys on predecessor run id and the failure is an insert-time constraint violation, not a duplicate transition.

The root mistake is the one from question 2: the *first* time atomic succession needs a piece of logic that already exists as a `Store` method — `transition()` with its CAS-on-version, most likely — you'll call the method instead of re-expressing the UPDATE against the transaction's connection, because "it already works and has tests." Every write in a succession unit must go through the one yielded connection, full stop, which means duplicating ~10 lines of the CAS UPDATE inside your succession function rather than reusing the method. That duplication will feel wrong. It isn't. The symptom that tells you you've slipped: any test where you kill the process mid-succession and the *restart* path raises `sqlite3.IntegrityError: UNIQUE constraint failed: runs.bot_id` instead of cleanly no-op'ing. Write that kill-and-restart test before you write the succession function.
4/5 harnesses answered.

