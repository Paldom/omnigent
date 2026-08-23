"""Durable home for bots, their definitions, and the vendor gates they share.

Sits beside :class:`army.store.Store` in the same SQLite file rather than in a
database of its own. That is not tidiness — it is the only way the succession
write can be atomic. Ending an iteration and scheduling the bot's next one
touch ``runs`` and ``bots``, and two databases cannot share a transaction, so a
crash between them would either repeat the iteration or lose it.

Composition rather than inheritance: a :class:`BotStore` holds a ``Store`` and
borrows its connection policy. Subclassing would put twelve tables behind one
class and make "what owns run state" ambiguous, which is the question ``army/``
exists to answer unambiguously.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any

from army.bots.model import (
    Bot,
    BotRevision,
    BotStatus,
    RunOutcome,
    WakePolicy,
    WakeReason,
    assert_legal_bot_move,
    dumps,
)
from army.bots.schedule import Wake
from army.state import loads
from army.store import ConcurrentTransition, Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id                  TEXT PRIMARY KEY,
    slug                TEXT NOT NULL UNIQUE,
    display_name        TEXT NOT NULL,
    title               TEXT,
    persona             TEXT NOT NULL,
    mission             TEXT NOT NULL,
    workload            TEXT NOT NULL,
    workload_config     TEXT NOT NULL DEFAULT '{}',
    harness             TEXT,
    wake                TEXT NOT NULL,
    status              TEXT NOT NULL,
    next_due_at         INTEGER,
    wake_reason         TEXT,
    idle_streak         INTEGER NOT NULL DEFAULT 0,
    error_streak        INTEGER NOT NULL DEFAULT 0,
    last_outcome        TEXT,
    current_revision_id TEXT,
    parent_bot_id       TEXT,
    root_bot_id         TEXT,
    depth               INTEGER NOT NULL DEFAULT 0,
    expires_at          INTEGER,
    created_by          TEXT NOT NULL,
    workspace           TEXT,
    browser_profile     TEXT,
    docs_ref            TEXT,
    version             INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);
-- The wake scan's only index. It answers "which active bots are due?" without
-- reading the fleet, which is the query every tick runs.
CREATE INDEX IF NOT EXISTS ix_bots_due ON bots (status, next_due_at);
CREATE INDEX IF NOT EXISTS ix_bots_lineage ON bots (root_bot_id, depth);

CREATE TABLE IF NOT EXISTS bot_revisions (
    id         TEXT PRIMARY KEY,
    bot_id     TEXT NOT NULL,
    rev        INTEGER NOT NULL,
    definition TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (bot_id, rev)
);

-- Vendor cooldown, durable. The in-memory Lane.cooldown_until is rebuilt empty
-- on every start, so after a crash every bot on a limited vendor is instantly
-- due again — the exact quota spin the outcome model exists to prevent.
CREATE TABLE IF NOT EXISTS provider_gates (
    vendor        TEXT PRIMARY KEY,
    blocked_until INTEGER NOT NULL,
    reason        TEXT,
    version       INTEGER NOT NULL DEFAULT 0
);
"""

#: ``REFERENCES`` clauses are omitted on purpose. ``army/store.py`` opens every
#: connection without ``PRAGMA foreign_keys=ON``, so they would enforce nothing
#: while reading as though they did. Lineage and ownership are checked in code,
#: where the check actually runs.
_FOREIGN_KEYS_ARE_ENFORCED_IN_CODE = True


class BotStore:
    """Bots, their revisions, and the shared vendor gates.

    :param store: The run store whose database this shares. Its schema is
        already bootstrapped by the time this constructor runs.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        with store.atomic() as conn:
            # Statement by statement, not executescript(): that helper issues an
            # implicit COMMIT before it runs, which would end the transaction
            # this is inside and leave the rest of the schema outside it.
            for statement in _statements(_SCHEMA):
                conn.execute(statement)

    def _tx(self, conn: sqlite3.Connection | None) -> AbstractContextManager[sqlite3.Connection]:
        """Join the caller's transaction, or open one for this write alone."""
        return nullcontext(conn) if conn is not None else self.store.atomic()

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        """
        One transaction spanning runs and bots.

        This is the succession primitive. The terminal transition, the command
        consume and the successor wake all take the connection this yields, so
        a crash lands before all three or after all three.
        """
        with self.store.atomic() as conn:
            yield conn

    # ── definition ────────────────────────────────────────────────

    def create(self, bot: Bot, *, conn: sqlite3.Connection | None = None) -> Bot:
        """
        Insert a bot and the first snapshot of its definition.

        Both in one transaction: a bot whose ``current_revision_id`` points at
        nothing has no definition for a run to pin, and a revision belonging to
        no bot is unreachable. Neither half is useful alone.

        :param bot: The bot, normally from :meth:`army.bots.model.Bot.new`.
        :param conn: Join an open transaction, or ``None``.
        :returns: The bot, with :attr:`Bot.current_revision_id` filled in.
        :raises ConcurrentTransition: If the slug is already taken.
        """
        revision = BotRevision.of(bot, 1, created_by=bot.created_by, now=bot.created_at)
        bot.current_revision_id = revision.id
        try:
            with self._tx(conn) as conn:
                conn.execute(_INSERT_BOT, _bot_params(bot))
                conn.execute(_INSERT_REVISION, _revision_params(revision))
        except sqlite3.IntegrityError as exc:
            raise ConcurrentTransition(f"a bot named {bot.slug!r} already exists") from exc
        return bot

    def revise(
        self,
        bot: Bot,
        *,
        created_by: str,
        now: int,
        conn: sqlite3.Connection | None = None,
    ) -> Bot:
        """
        Snapshot an edited definition as the next revision and point the bot at it.

        The previous revision is left alone. Runs that pinned it still describe
        what actually happened, which is the whole reason revisions exist:
        without them, editing a persona silently rewrites the meaning of that
        bot's own history.

        :param bot: The bot carrying its edited definition and the version it
            was read at.
        :param created_by: Who made the edit.
        :param now: Epoch seconds.
        :param conn: Join an open transaction, or ``None``.
        :returns: The bot, at its new version and revision.
        :raises ConcurrentTransition: If someone else edited it first.
        """
        with self._tx(conn) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(rev), 0) AS top FROM bot_revisions WHERE bot_id = ?",
                (bot.id,),
            ).fetchone()
            revision = BotRevision.of(bot, int(row["top"]) + 1, created_by=created_by, now=now)
            conn.execute(_INSERT_REVISION, _revision_params(revision))
            self._cas(
                conn,
                bot,
                "current_revision_id = ?, slug = ?, display_name = ?, title = ?,"
                " persona = ?, mission = ?, workload = ?, workload_config = ?,"
                " harness = ?, wake = ?, workspace = ?, browser_profile = ?,"
                " docs_ref = ?, expires_at = ?",
                (
                    revision.id,
                    bot.slug,
                    bot.display_name,
                    bot.title,
                    bot.persona,
                    bot.mission,
                    bot.workload,
                    dumps(bot.workload_config),
                    bot.harness,
                    dumps(bot.wake.to_dict()),
                    bot.workspace,
                    bot.browser_profile,
                    bot.docs_ref,
                    bot.expires_at,
                ),
                now=now,
            )
        bot.current_revision_id = revision.id
        bot.version += 1
        bot.updated_at = now
        return bot

    def revisions(self, bot_id: str) -> list[BotRevision]:
        """
        Every snapshot of a bot's definition, oldest first.

        :param bot_id: The bot.
        :returns: Its revisions.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT * FROM bot_revisions WHERE bot_id = ? ORDER BY rev", (bot_id,)
            ).fetchall()
        return [_row_to_revision(row) for row in rows]

    def revision(self, revision_id: str) -> BotRevision | None:
        """
        Load one revision by id, so a run can be read under the definition it ran on.

        :param revision_id: The revision.
        :returns: It, or ``None``.
        """
        with self.store.atomic() as conn:
            row = conn.execute(
                "SELECT * FROM bot_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        return _row_to_revision(row) if row is not None else None

    # ── reading ───────────────────────────────────────────────────

    def get(self, bot_id: str, *, conn: sqlite3.Connection | None = None) -> Bot | None:
        """
        Load one bot by id.

        :param bot_id: The bot.
        :param conn: Join an open transaction, or ``None``.
        :returns: The bot, or ``None``.
        """
        with self._tx(conn) as conn:
            row = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        return _row_to_bot(row) if row is not None else None

    def by_slug(self, slug: str) -> Bot | None:
        """
        Load one bot by its addressable name.

        :param slug: The slug.
        :returns: The bot, or ``None``.
        """
        with self.store.atomic() as conn:
            row = conn.execute("SELECT * FROM bots WHERE slug = ?", (slug,)).fetchone()
        return _row_to_bot(row) if row is not None else None

    def list(self, *, status: BotStatus | None = None) -> list[Bot]:
        """
        Every bot, or every bot in one lifecycle state, by slug.

        :param status: Restrict to one lifecycle state, or ``None`` for all.
        :returns: The bots, ordered by slug so the roster is stable between reads.
        """
        sql = "SELECT * FROM bots"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status.value,)
        with self.store.atomic() as conn:
            rows = conn.execute(sql + " ORDER BY slug", params).fetchall()
        return [_row_to_bot(row) for row in rows]

    def children(self, parent_bot_id: str) -> list[Bot]:
        """
        Bots this one spawned, for the fan-out cap and the retire cascade.

        :param parent_bot_id: The parent.
        :returns: Its direct children.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT * FROM bots WHERE parent_bot_id = ? ORDER BY created_at", (parent_bot_id,)
            ).fetchall()
        return [_row_to_bot(row) for row in rows]

    def due(self, *, now: int, limit: int = 50) -> list[Bot]:
        """
        Active bots whose wake has arrived and which have no live run.

        One query, and it is the whole scan. The live-run condition is a
        correlated ``NOT EXISTS`` rather than a join so a bot with a finished
        run is not filtered out by it, and the partial unique index makes the
        race that slips past it harmless anyway.

        :param now: Epoch seconds.
        :param limit: Most bots to return in one tick, so a large fleet does
            not turn one tick into an unbounded amount of work.
        :returns: Due bots, longest-waiting first.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT * FROM bots WHERE status = ? AND next_due_at IS NOT NULL"
                " AND next_due_at <= ? AND NOT EXISTS ("
                "  SELECT 1 FROM runs WHERE runs.bot_id = bots.id"
                "  AND runs.state NOT IN ('continue','completed','failed'))"
                " ORDER BY next_due_at, id LIMIT ?",
                (BotStatus.ACTIVE.value, now, limit),
            ).fetchall()
        return [_row_to_bot(row) for row in rows]

    def live_run_states(self) -> dict[str, str]:
        """
        The state of each bot's in-flight run, for deriving status.

        :returns: ``{bot_id: run_state}`` for every bot with a non-terminal run.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT bot_id, state FROM runs WHERE bot_id IS NOT NULL"
                " AND state NOT IN ('continue','completed','failed')"
            ).fetchall()
        return {row["bot_id"]: row["state"] for row in rows}

    def live_run_ids(self) -> dict[str, str]:
        """
        The id of each bot's in-flight run, so the roster can link to it.

        :returns: ``{bot_id: run_id}`` for every bot with a non-terminal run.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT bot_id, id FROM runs WHERE bot_id IS NOT NULL"
                " AND state NOT IN ('continue','completed','failed')"
            ).fetchall()
        return {row["bot_id"]: row["id"] for row in rows}

    def runs_for(self, bot_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """
        A bot's recent iterations, newest first, for its ledger.

        Returns plain dicts rather than :class:`~army.state.Run` objects: the
        caller wants a display row, and rebuilding the full object would decode
        three JSON columns nobody is going to read.

        :param bot_id: The bot.
        :param limit: How many.
        :returns: One dict per run.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT id, state, outcome, revision_id, created_at, updated_at,"
                " terminal_reason FROM runs WHERE bot_id = ?"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (bot_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def stalled(self, *, now: int) -> list[Bot]:  # noqa: ARG002
        """
        Active bots that nothing will ever wake, and that nothing is waiting for.

        The backstop for a lost succession. A bot with no live run, no next
        wake, and a wake reason that is not one of the two legitimate "no
        schedule" cases has fallen through a crack — and without this it falls
        through it silently, which is the worst way to lose a bot.

        :param now: Epoch seconds, unused today but part of the contract so a
            TTL check can join this scan later.
        :returns: Bots to raise as an operator alarm.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT * FROM bots WHERE status = ? AND next_due_at IS NULL"
                " AND COALESCE(wake_reason, '') NOT IN (?, ?)"
                " AND NOT EXISTS ("
                "  SELECT 1 FROM runs WHERE runs.bot_id = bots.id"
                "  AND runs.state NOT IN ('continue','completed','failed'))"
                " ORDER BY updated_at",
                (BotStatus.ACTIVE.value, WakeReason.EVENT.value, WakeReason.MANUAL.value),
            ).fetchall()
        # A bot blocked on a person is legitimately unscheduled — but only while
        # something is actually pending for it. That check needs the approvals
        # table, so the caller filters; this returns the candidates.
        return [_row_to_bot(row) for row in rows]

    # ── writing ───────────────────────────────────────────────────

    def record_wake(
        self,
        bot: Bot,
        wake: Wake,
        outcome: RunOutcome | None,
        *,
        now: int,
        conn: sqlite3.Connection | None = None,
    ) -> Bot:
        """
        Write the successor wake, the streaks and the last outcome, as one CAS.

        Pass the *conn* from :meth:`atomic` and this lands in the same
        transaction as the run's terminal transition. That pairing is the
        succession contract: there is no second "bump the bot" path to forget,
        which is exactly how the first draft lost bots forever.

        :param bot: The bot, carrying the version it was read at.
        :param wake: What the scheduler decided.
        :param outcome: The iteration's classification, or ``None`` when no run
            produced one — an activation, say.
        :param now: Epoch seconds.
        :param conn: Join an open transaction, or ``None``.
        :returns: The bot, updated.
        :raises ConcurrentTransition: If someone else moved it first.
        """
        with self._tx(conn) as conn:
            self._cas(
                conn,
                bot,
                "next_due_at = ?, wake_reason = ?, idle_streak = ?, error_streak = ?,"
                " last_outcome = ?",
                (
                    wake.next_due_at,
                    wake.reason.value if wake.reason is not None else None,
                    wake.idle_streak,
                    wake.error_streak,
                    outcome.value if outcome is not None else bot.last_outcome,
                ),
                now=now,
            )
        bot.next_due_at = wake.next_due_at
        bot.wake_reason = wake.reason
        bot.idle_streak = wake.idle_streak
        bot.error_streak = wake.error_streak
        if outcome is not None:
            bot.last_outcome = outcome
        bot.version += 1
        bot.updated_at = now
        return bot

    def set_status(
        self,
        bot: Bot,
        target: BotStatus,
        *,
        now: int,
        wake: Wake | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Bot:
        """
        Move a bot's lifecycle, and set its first wake if it is being activated.

        Activation and the wake it implies are one write. Splitting them leaves
        a window where a bot is ``ACTIVE`` with no ``next_due_at``, which the
        stalled scan would correctly report as broken.

        :param bot: The bot, carrying the version it was read at.
        :param target: Where to move it.
        :param now: Epoch seconds.
        :param wake: The wake to install with the move, normally from
            :func:`army.bots.schedule.first_wake` on an activation.
        :param conn: Join an open transaction, or ``None``.
        :returns: The bot, updated.
        :raises IllegalBotMove: If the move is not defined.
        :raises ConcurrentTransition: If someone else moved it first.
        """
        assert_legal_bot_move(bot.status, target)
        columns = "status = ?"
        params: list[Any] = [target.value]
        if wake is not None:
            columns += ", next_due_at = ?, wake_reason = ?, idle_streak = ?, error_streak = ?"
            params += [
                wake.next_due_at,
                wake.reason.value if wake.reason is not None else None,
                wake.idle_streak,
                wake.error_streak,
            ]
        elif target is not BotStatus.ACTIVE:
            # A bot that is not active must not keep a due time: the scan reads
            # status first, but a stale time makes the roster claim a paused bot
            # is about to run.
            columns += ", next_due_at = NULL"
        with self._tx(conn) as conn:
            self._cas(conn, bot, columns, tuple(params), now=now)
        bot.status = target
        if wake is not None:
            bot.next_due_at = wake.next_due_at
            bot.wake_reason = wake.reason
            bot.idle_streak = wake.idle_streak
            bot.error_streak = wake.error_streak
        elif target is not BotStatus.ACTIVE:
            bot.next_due_at = None
        bot.version += 1
        bot.updated_at = now
        return bot

    def _cas(
        self,
        conn: sqlite3.Connection,
        bot: Bot,
        assignments: str,
        params: tuple[Any, ...],
        *,
        now: int,
    ) -> None:
        """
        Apply one conditional update, bumping the fencing version.

        :param conn: Open connection.
        :param bot: The bot, carrying the version to compare against.
        :param assignments: The ``SET`` clause, without ``version`` or
            ``updated_at`` — this adds both.
        :param params: Values for *assignments*.
        :param now: Epoch seconds.
        :raises ConcurrentTransition: If the version moved.
        """
        cursor = conn.execute(
            f"UPDATE bots SET {assignments}, version = ?, updated_at = ?"
            " WHERE id = ? AND version = ?",
            (*params, bot.version + 1, now, bot.id, bot.version),
        )
        if cursor.rowcount != 1:
            raise ConcurrentTransition(
                f"bot {bot.slug} moved on from version {bot.version}; re-read before deciding"
            )

    # ── vendor gates ──────────────────────────────────────────────

    def block_vendor(
        self,
        vendor: str,
        until: int,
        reason: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """
        Cool a whole vendor lane, durably.

        Latest wins, and only forward: two bots reporting the same limit must
        not shorten each other's cooldown, which an unconditional write would
        let the second one do.

        :param vendor: Harness id, e.g. ``"claude-native"``.
        :param until: Epoch seconds the lane reopens.
        :param reason: What the vendor said, for the operator.
        :param conn: Join an open transaction, or ``None``.
        """
        with self._tx(conn) as conn:
            conn.execute(
                "INSERT INTO provider_gates (vendor, blocked_until, reason, version)"
                " VALUES (?,?,?,0) ON CONFLICT(vendor) DO UPDATE SET"
                " blocked_until = MAX(blocked_until, excluded.blocked_until),"
                " reason = excluded.reason, version = version + 1",
                (vendor, until, reason),
            )

    def blocked_vendors(self, *, now: int) -> dict[str, int]:
        """
        Vendors still cooling, and when each reopens.

        :param now: Epoch seconds.
        :returns: ``{vendor: blocked_until}``, empty when everything is open.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT vendor, blocked_until FROM provider_gates WHERE blocked_until > ?",
                (now,),
            ).fetchall()
        return {row["vendor"]: row["blocked_until"] for row in rows}


_INSERT_BOT = (
    "INSERT INTO bots (id, slug, display_name, title, persona, mission, workload,"
    " workload_config, harness, wake, status, next_due_at, wake_reason, idle_streak,"
    " error_streak, last_outcome, current_revision_id, parent_bot_id, root_bot_id,"
    " depth, expires_at, created_by, workspace, browser_profile, docs_ref, version,"
    " created_at, updated_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_INSERT_REVISION = (
    "INSERT INTO bot_revisions (id, bot_id, rev, definition, created_by, created_at)"
    " VALUES (?,?,?,?,?,?)"
)


def _bot_params(bot: Bot) -> tuple[Any, ...]:
    """Flatten a bot into the column order of :data:`_INSERT_BOT`."""
    return (
        bot.id,
        bot.slug,
        bot.display_name,
        bot.title,
        bot.persona,
        bot.mission,
        bot.workload,
        dumps(bot.workload_config),
        bot.harness,
        dumps(bot.wake.to_dict()),
        bot.status.value,
        bot.next_due_at,
        bot.wake_reason.value if bot.wake_reason is not None else None,
        bot.idle_streak,
        bot.error_streak,
        bot.last_outcome.value if bot.last_outcome is not None else None,
        bot.current_revision_id,
        bot.parent_bot_id,
        bot.root_bot_id,
        bot.depth,
        bot.expires_at,
        bot.created_by,
        bot.workspace,
        bot.browser_profile,
        bot.docs_ref,
        bot.version,
        bot.created_at,
        bot.updated_at,
    )


def _revision_params(revision: BotRevision) -> tuple[Any, ...]:
    """Flatten a revision into the column order of :data:`_INSERT_REVISION`."""
    return (
        revision.id,
        revision.bot_id,
        revision.rev,
        dumps(revision.definition),
        revision.created_by,
        revision.created_at,
    )


def _row_to_bot(row: sqlite3.Row) -> Bot:
    """Rebuild a :class:`Bot` from its row."""
    return Bot(
        id=row["id"],
        slug=row["slug"],
        display_name=row["display_name"],
        title=row["title"],
        persona=row["persona"],
        mission=row["mission"],
        workload=row["workload"],
        workload_config=loads(row["workload_config"], {}),
        harness=row["harness"],
        wake=WakePolicy.from_dict(loads(row["wake"], {"kind": "manual"})),
        status=BotStatus(row["status"]),
        next_due_at=row["next_due_at"],
        wake_reason=WakeReason(row["wake_reason"]) if row["wake_reason"] else None,
        idle_streak=row["idle_streak"],
        error_streak=row["error_streak"],
        last_outcome=RunOutcome(row["last_outcome"]) if row["last_outcome"] else None,
        current_revision_id=row["current_revision_id"],
        parent_bot_id=row["parent_bot_id"],
        root_bot_id=row["root_bot_id"],
        depth=row["depth"],
        expires_at=row["expires_at"],
        created_by=row["created_by"],
        workspace=row["workspace"],
        browser_profile=row["browser_profile"],
        docs_ref=row["docs_ref"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_revision(row: sqlite3.Row) -> BotRevision:
    """Rebuild a :class:`BotRevision` from its row."""
    return BotRevision(
        id=row["id"],
        bot_id=row["bot_id"],
        rev=row["rev"],
        definition=loads(row["definition"], {}),
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _statements(script: str) -> list[str]:
    """
    Split a schema script into statements, dropping comments and blanks.

    Only safe because this file's own DDL is the only input: there are no
    string literals containing a semicolon, and no triggers with bodies.

    :param script: The schema text.
    :returns: One executable statement per entry.
    """
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


def new_id() -> str:
    """A fresh identifier, in the same shape the run store uses."""
    return uuid.uuid4().hex
