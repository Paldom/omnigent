"""SQLite persistence for runs, commands and effects.

Plain ``sqlite3`` rather than an ORM: the whole schema is three tables, the
control plane is single-box by design, and the property that matters —
compare-and-swap on a version column — is one ``UPDATE ... WHERE version = ?``.

Every write is its own transaction. The one place that needs two writes to be
atomic (consume a command *and* apply the transition it caused) gets them in a
single transaction, so a crash in between replays the command instead of losing
the answer it carried.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from typing import Any

from army.state import (
    RESUMABLE,
    Command,
    CommandKind,
    Run,
    RunState,
    assert_legal,
    dumps,
    loads,
)

#: How long a connection waits for a lock before giving up. Two processes share
#: this file — the supervisor loop and the CLI — so contention is normal and a
#: connection with no timeout turns every overlap into "database is locked".
BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    workflow        TEXT NOT NULL,
    state           TEXT NOT NULL,
    version         INTEGER NOT NULL,
    attempt         INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    payload         TEXT NOT NULL,
    artifacts       TEXT NOT NULL,
    outstanding     TEXT NOT NULL,
    approval_id     TEXT,
    terminal_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_state ON runs (state, created_at);
CREATE INDEX IF NOT EXISTS ix_runs_approval ON runs (approval_id);

CREATE TABLE IF NOT EXISTS commands (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    consumed_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_commands_pending ON commands (run_id, consumed_at, created_at);

CREATE TABLE IF NOT EXISTS effects (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    idempotency   TEXT NOT NULL UNIQUE,
    state         TEXT NOT NULL,
    request       TEXT NOT NULL,
    result        TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_effects_run ON effects (run_id, state);

CREATE TABLE IF NOT EXISTS used_grants (
    id          TEXT PRIMARY KEY,
    operation   TEXT NOT NULL,
    consumed_at INTEGER NOT NULL
);
"""

#: Columns added to ``runs`` after the first release, as
#: ``(name, declaration)``. ``CREATE TABLE IF NOT EXISTS`` cannot add a column
#: to a table that already exists, and a live ``army.db`` predates all three —
#: so they are detected and added rather than declared above.
#:
#: Every one is nullable. That is what lets an older binary keep writing to a
#: newer database: its INSERT names its own columns and SQLite fills these with
#: NULL, which reads back as "this run had no bot", which is true.
_RUN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("bot_id", "TEXT"),
    ("revision_id", "TEXT"),
    ("outcome", "TEXT"),
)

#: One live run per bot, enforced where it cannot be forgotten.
#:
#: ``paused`` is deliberately *inside* the index. It is resumable, so excluding
#: it would let a tick open a second run while the first sits paused, and the
#: resume would then collide on an IntegrityError the CAS path does not expect.
#: Keeping it in also gives "pause this run" the meaning people assume: the bot
#: stops, rather than quietly starting the next iteration.
_ONE_LIVE_RUN_PER_BOT = (
    "CREATE UNIQUE INDEX IF NOT EXISTS one_live_run_per_bot ON runs (bot_id)"
    " WHERE bot_id IS NOT NULL AND state NOT IN ('continue','completed','failed')"
)

#: The ledger walk. The partial index above covers only *live* rows, so reading
#: a bot's history would otherwise scan `runs` — which grows one row per
#: iteration with no retention policy, so it is fine on day one and a table
#: scan by the end of the month.
_RUNS_BY_BOT = "CREATE INDEX IF NOT EXISTS ix_runs_bot ON runs (bot_id, created_at DESC)"


def add_missing_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> list[str]:
    """
    Add columns a live database predates, and nothing else.

    This is the whole migration mechanism, and keeping it this small is
    deliberate. A version-number ladder would need every step written, ordered
    and never re-run; asking the table what it already has is idempotent by
    construction, so a fresh database and a two-year-old one end up identical
    and running it twice costs one ``PRAGMA``.

    What it cannot do is change or drop a column, which is the correct
    limitation: those need the data rewritten, and a schema helper that quietly
    rewrites data is how a Saturday disappears.

    :param conn: An open connection, inside whatever transaction the caller has.
    :param table: Table to extend.
    :param columns: ``(name, declaration)`` pairs. Declarations must be
        nullable so an older binary can still write the row.
    :returns: The names actually added, for a log line.
    """
    present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    added = []
    for name, declaration in columns:
        if name in present:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        added.append(name)
    return added


class ConcurrentTransition(RuntimeError):
    """Raised when a transition loses the compare-and-swap.

    Someone else advanced the run between the read and the write. The caller
    should re-read and decide again rather than retrying blind — the run may
    now be somewhere its old decision no longer makes sense.
    """


class Store:
    """Durable home for the control plane's own state.

    :param path: SQLite file. Its parent directory is created if missing.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Outside a transaction, deliberately: `journal_mode` cannot be changed
        # inside one, and `executescript` issues its own COMMIT. Schema setup is
        # the one place that is not a transaction, and every statement in it is
        # idempotent so a torn setup simply re-runs.
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            # WAL so the supervisor writing does not block the CLI reading
            # status, which is the only concurrency this thing has.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            add_missing_columns(conn, "runs", _RUN_COLUMNS)
            # After the columns exist, never before: the index names one.
            conn.execute(_ONE_LIVE_RUN_PER_BOT)
            conn.execute(_RUNS_BY_BOT)
        finally:
            conn.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a transaction that commits on success and rolls back on error.

        One policy for every caller, and it is the strict one. A mix of
        deferred and immediate transactions across two processes is the classic
        SQLite deadlock: the CLI begins deferred and takes SHARED, the
        supervisor begins immediate and takes RESERVED, the CLI's write then
        waits for RESERVED while the supervisor's commit waits for the CLI's
        SHARED to clear. Neither moves, and one of them gets
        ``database is locked`` — during a succession, which is the one write
        that must not be dropped.

        So there is no deferred path to fall into. Reads pay for a write lock
        they do not need, which at one box and ten bots costs nothing
        measurable and removes an entire class of 3am failure.
        """
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # Per-connection, not per-database: a fresh connection with no timeout
        # turns every moment of contention into an exception instead of a wait.
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        """
        Run several writes as one transaction.

        Called on its own, every method below opens its own transaction — which
        is right for a single write and wrong for a succession: ending one
        iteration and scheduling the next are two writes that a crash between
        would either repeat or lose. Pass the connection this yields as each
        call's *conn* and they land together or not at all.

        **Nothing inside may do I/O.** Not an HTTP call to Omnigent, not a
        subprocess, not a queue file. A transaction holds SQLite's write lock,
        so a network call inside one turns ``army status`` into a hang; worse,
        a crash mid-call cannot roll the call back, so the effect happened and
        the row says it did not. Write the intent, commit, do the I/O, record
        the result in a new transaction — which is what ``effects`` is for.

        **Nothing inside may open another one.** These are real transactions on
        real connections, so a nested call self-deadlocks against its own outer
        lock and waits out the busy timeout before failing. Every store method
        takes a *conn* for exactly this reason: inside a transaction, pass it.

        :returns: A connection that commits on a clean exit and rolls back on
            an exception.
        """
        with self._connect() as conn:
            yield conn

    def _tx(self, conn: sqlite3.Connection | None) -> AbstractContextManager[sqlite3.Connection]:
        """
        Join the caller's transaction, or open one for this write alone.

        :param conn: A connection from :meth:`atomic`, or ``None``.
        :returns: A context manager yielding the connection to use. When the
            caller supplied one, exiting it commits nothing — the outer
            :meth:`atomic` owns that decision.
        """
        return nullcontext(conn) if conn is not None else self._connect()

    # ── runs ──────────────────────────────────────────────────────

    def create_run(self, run: Run, *, conn: sqlite3.Connection | None = None) -> Run:
        """
        Insert a new run.

        :param run: The run to persist, normally from :meth:`Run.new`.
        :param conn: Join an open transaction from :meth:`atomic`, or ``None``
            to write on its own.
        :returns: The same run.
        :raises ConcurrentTransition: If this bot already has a live run. The
            partial unique index is what enforces one iteration per bot, so two
            ticks racing the same due bot produce one run and one loser — the
            same shape as losing a compare-and-swap, and handled the same way.
        """
        try:
            with self._tx(conn) as conn:
                conn.execute(
                    "INSERT INTO runs (id, workflow, state, version, attempt, created_at,"
                    " updated_at, payload, artifacts, outstanding, approval_id,"
                    " terminal_reason, bot_id, revision_id, outcome)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run.id,
                        run.workflow,
                        run.state.value,
                        run.version,
                        run.attempt,
                        run.created_at,
                        run.updated_at,
                        dumps(run.payload),
                        dumps(run.artifacts),
                        dumps(run.outstanding),
                        run.approval_id,
                        run.terminal_reason,
                        run.bot_id,
                        run.revision_id,
                        run.outcome,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConcurrentTransition(
                f"bot {run.bot_id} already has a live run; another tick won"
            ) from exc
        return run

    def get_run(self, run_id: str, *, conn: sqlite3.Connection | None = None) -> Run | None:
        """
        Load one run by id.

        :param run_id: Run to load.
        :param conn: Join an open transaction from :meth:`atomic`, or ``None``.
        :returns: The run, or ``None`` if there is no such row.
        """
        with self._tx(conn) as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row is not None else None

    def active_runs(self, *, workflow: str | None = None) -> list[Run]:
        """
        Load every run the supervisor still has work to do on, oldest first.

        This is the whole of restart recovery: the states are the recovery
        plan, so there is no separate sweep to keep in step with them.

        :param workflow: Restrict to one loop definition, or ``None`` for all.
        :returns: Non-terminal runs, oldest first.
        """
        working = [s.value for s in RunState if s not in _TERMINAL_VALUES]
        placeholders = ",".join("?" * len(working))
        resumable = [s.value for s in RESUMABLE]
        resumable_placeholders = ",".join("?" * len(resumable))
        # A paused run is only picked up when a human has actually asked for
        # it. Without the unconsumed-command condition it would either be
        # invisible forever (the bug this replaces) or spin on every tick.
        sql = (
            f"SELECT runs.* FROM runs WHERE (runs.state IN ({placeholders})"
            f" OR (runs.state IN ({resumable_placeholders}) AND EXISTS ("
            "  SELECT 1 FROM commands WHERE commands.run_id = runs.id"
            "  AND commands.consumed_at IS NULL)))"
        )
        params: list[Any] = [*working, *resumable]
        if workflow is not None:
            sql += " AND runs.workflow = ?"
            params.append(workflow)
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY runs.created_at, runs.id", params).fetchall()
        return [_row_to_run(row) for row in rows]

    def list_runs(self, *, limit: int = 50) -> list[Run]:
        """
        Load the most recent runs regardless of state, newest first.

        :param limit: How many to return.
        :returns: Runs, newest first.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def run_awaiting(self, approval_id: str) -> Run | None:
        """
        Find the run parked on a given approval.

        :param approval_id: An Omnigent elicitation id.
        :returns: The run waiting on it, or ``None``.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE approval_id = ? AND state = ?",
                (approval_id, RunState.WAITING_HUMAN.value),
            ).fetchone()
        return _row_to_run(row) if row is not None else None

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
        outcome: str | None = None,
        now: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Run:
        """
        Advance a run by exactly one legal move, or fail.

        The write is conditional on the version *run* was read at, so two
        supervisors that both decided to move it produce one transition and one
        :class:`ConcurrentTransition`. Passing *consume* applies the command
        and the transition in the same transaction, so an answer cannot be
        marked used by a move that did not land.

        :param run: The run as read, carrying the version to compare against.
        :param target: Where to move it.
        :param artifacts: Replacement artifacts, or ``None`` to keep.
        :param outstanding: Replacement outstanding sessions, or ``None``.
        :param approval_id: Approval to park on, or ``None`` to keep.
        :param terminal_reason: Why it ended, for a terminal move.
        :param attempt: Replacement attempt count, or ``None`` to keep.
        :param consume: A command to mark consumed atomically with the move.
        :param outcome: How the iteration classified itself, set on the move
            that ends it. Recorded with the transition rather than after it,
            because a terminal run with no outcome is indistinguishable from
            one that did work — and the scheduler would wake the bot at its
            floor forever on that reading.
        :param now: Unix epoch seconds; defaults to the clock.
        :param conn: Join an open transaction from :meth:`atomic`, so the move
            lands with whatever else that transaction is writing — a bot's
            successor wake, say.
        :returns: The run as it now stands.
        :raises IllegalTransition: If the move is not defined.
        :raises ConcurrentTransition: If someone else moved first.
        """
        assert_legal(run.state, target)
        stamp = int(time.time()) if now is None else now
        updated = Run(
            id=run.id,
            workflow=run.workflow,
            state=target,
            version=run.version + 1,
            attempt=run.attempt if attempt is None else attempt,
            created_at=run.created_at,
            updated_at=stamp,
            payload=run.payload,
            artifacts=run.artifacts if artifacts is None else artifacts,
            outstanding=run.outstanding if outstanding is None else outstanding,
            approval_id=run.approval_id if approval_id is None else approval_id,
            terminal_reason=terminal_reason,
            bot_id=run.bot_id,
            revision_id=run.revision_id,
            outcome=run.outcome if outcome is None else outcome,
        )
        try:
            with self._tx(conn) as conn:
                cursor = conn.execute(
                    "UPDATE runs SET state = ?, version = ?, attempt = ?, updated_at = ?,"
                    " artifacts = ?, outstanding = ?, approval_id = ?, terminal_reason = ?,"
                    " outcome = ? WHERE id = ? AND version = ?",
                    (
                        updated.state.value,
                        updated.version,
                        updated.attempt,
                        updated.updated_at,
                        dumps(updated.artifacts),
                        dumps(updated.outstanding),
                        updated.approval_id,
                        updated.terminal_reason,
                        updated.outcome,
                        run.id,
                        run.version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConcurrentTransition(
                        f"run {run.id} moved on from version {run.version};"
                        " re-read before deciding"
                    )
                if consume is not None:
                    conn.execute(
                        "UPDATE commands SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                        (stamp, consume.id),
                    )
        except sqlite3.IntegrityError as exc:
            # Resuming a paused run puts it back inside the one-live-run index.
            # If a tick opened a second run while it sat paused, that resume is
            # the collision — and it is the same answer as losing a CAS: re-read,
            # because the world moved.
            raise ConcurrentTransition(
                f"run {run.id} cannot move to {target.value};"
                f" bot {run.bot_id} has another live run"
            ) from exc
        return updated

    # ── commands ──────────────────────────────────────────────────

    def record_command(
        self, command: Command, *, conn: sqlite3.Connection | None = None
    ) -> Command:
        """
        Persist a command so it outlives whatever asked for it.

        :param command: The command, normally from :meth:`Command.new`.
        :param conn: Join an open transaction from :meth:`atomic`, or ``None``.
        :returns: The same command.
        """
        with self._tx(conn) as conn:
            conn.execute(
                "INSERT INTO commands (id, run_id, kind, payload, created_at, consumed_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    command.id,
                    command.run_id,
                    command.kind.value,
                    dumps(command.payload),
                    command.created_at,
                    command.consumed_at,
                ),
            )
        return command

    def next_command(
        self, run_id: str, *, conn: sqlite3.Connection | None = None
    ) -> Command | None:
        """
        Return the oldest unconsumed command for a run.

        :param run_id: Run to check.
        :param conn: Join an open transaction from :meth:`atomic`, or ``None``.
        :returns: The command, or ``None`` when nothing is outstanding.
        """
        with self._tx(conn) as conn:
            row = conn.execute(
                "SELECT * FROM commands WHERE run_id = ? AND consumed_at IS NULL"
                " ORDER BY created_at, id LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return Command(
            id=row["id"],
            run_id=row["run_id"],
            kind=CommandKind(row["kind"]),
            payload=loads(row["payload"], {}),
            created_at=row["created_at"],
            consumed_at=row["consumed_at"],
        )

    def consume_command(self, command: Command, *, now: int | None = None) -> None:
        """
        Mark a command consumed without moving the run.

        For a command that cannot apply where the run has ended up — a verdict
        that arrived after the branch was already paused, say. Leaving it
        unconsumed would wake the run later with an answer to a question that
        is no longer being asked.

        :param command: The command to retire.
        :param now: Unix epoch seconds; defaults to the clock.
        """
        stamp = int(time.time()) if now is None else now
        with self._connect() as conn:
            conn.execute(
                "UPDATE commands SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (stamp, command.id),
            )

    # ── effects ───────────────────────────────────────────────────

    def begin_effect(
        self,
        run_id: str,
        kind: str,
        idempotency: str,
        request: dict[str, Any],
        *,
        now: int | None = None,
    ) -> tuple[str, str]:
        """
        Claim an external side effect before performing it.

        Writing ``dispatched`` *before* the call is what makes an ambiguous
        failure detectable: a row left in ``dispatched`` by a crash means the
        call may or may not have happened, and must be reconciled against the
        outside world rather than blindly retried.

        The idempotency key is unique, so a re-run that re-derives the same key
        finds the existing row instead of making a second one.

        :param run_id: Run the effect belongs to.
        :param kind: What sort of effect, e.g. ``"merge_pr"``.
        :param idempotency: Stable key derived from the operation itself.
        :param request: What is about to be attempted, for reconciliation.
        :param now: Unix epoch seconds; defaults to the clock.
        :returns: ``(effect_id, state)`` — the state is ``"dispatched"`` for a
            fresh claim, or the existing row's state when one was already
            recorded for this key.
        """
        stamp = int(time.time()) if now is None else now
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, state FROM effects WHERE idempotency = ?", (idempotency,)
            ).fetchone()
            if row is not None:
                return row["id"], row["state"]
            effect_id = f"eff_{idempotency[:16]}_{stamp}"
            conn.execute(
                "INSERT INTO effects (id, run_id, kind, idempotency, state, request,"
                " result, attempts, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    effect_id,
                    run_id,
                    kind,
                    idempotency,
                    "dispatched",
                    dumps(request),
                    None,
                    1,
                    stamp,
                    stamp,
                ),
            )
        return effect_id, "dispatched"

    def finish_effect(
        self,
        effect_id: str,
        state: str,
        result: dict[str, Any] | None = None,
        *,
        now: int | None = None,
    ) -> None:
        """
        Record how an external side effect turned out.

        :param effect_id: The claim returned by :meth:`begin_effect`.
        :param state: ``"succeeded"``, ``"failed"`` or ``"outcome_unknown"``.
        :param result: Whatever the call returned, for the audit trail.
        :param now: Unix epoch seconds; defaults to the clock.
        """
        stamp = int(time.time()) if now is None else now
        with self._connect() as conn:
            conn.execute(
                "UPDATE effects SET state = ?, result = ?, updated_at = ? WHERE id = ?",
                (state, dumps(result) if result is not None else None, stamp, effect_id),
            )

    def consume_grant(self, grant_id: str, operation: str, *, now: int | None = None) -> bool:
        """
        Spend an owner grant, exactly once.

        An HMAC that merely *validates* is a capability, not a permission: it
        keeps working until it expires, so one approval of a transfer authorises
        that transfer for the rest of the hour. Recording the id under a primary
        key makes the second attempt lose — the insert is the lock.

        :param grant_id: The grant's nonce.
        :param operation: The verb, recorded for the audit trail.
        :param now: Unix epoch seconds; defaults to the clock.
        :returns: ``True`` if this call spent the grant, ``False`` if it was
            already spent.
        """
        stamp = int(time.time()) if now is None else now
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO used_grants (id, operation, consumed_at) VALUES (?,?,?)",
                    (grant_id, operation, stamp),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def unreconciled_effects(self) -> list[dict[str, Any]]:
        """
        Return effects whose outcome a restart left unknown.

        A row still in ``dispatched`` was claimed by a process that did not
        live to record the answer. Nothing may retry one of these until it has
        been checked against the system it acted on.

        :returns: One dict per unresolved effect.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM effects WHERE state = 'dispatched' ORDER BY created_at"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "idempotency": row["idempotency"],
                "request": loads(row["request"], {}),
                "attempts": row["attempts"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


_TERMINAL_VALUES = frozenset(
    {RunState.CONTINUE, RunState.PAUSED, RunState.COMPLETED, RunState.FAILED}
)


def _row_to_run(row: sqlite3.Row) -> Run:
    """Rebuild a :class:`Run` from its row."""
    return Run(
        id=row["id"],
        workflow=row["workflow"],
        state=RunState(row["state"]),
        version=row["version"],
        attempt=row["attempt"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        payload=loads(row["payload"], {}),
        artifacts=loads(row["artifacts"], {}),
        outstanding=loads(row["outstanding"], []),
        approval_id=row["approval_id"],
        terminal_reason=row["terminal_reason"],
        bot_id=_optional(row, "bot_id"),
        revision_id=_optional(row, "revision_id"),
        outcome=_optional(row, "outcome"),
    )


def _optional(row: sqlite3.Row, column: str) -> Any:
    """
    Read a column that a database written by an older binary may not have.

    ``SELECT *`` against a pre-migration file returns a row with no such key,
    and one missing column must not make a run unloadable — the run is what
    holds the loop's place.

    :param row: The row as read.
    :param column: Column name.
    :returns: The value, or ``None`` when the column is absent.
    """
    try:
        return row[column]
    except (IndexError, KeyError):
        return None
