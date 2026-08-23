"""What a bot is allowed to spend, and why creating a bot cannot create capacity.

Two failures need the same mechanism.

**The poison mission.** A bot that reports ``WORK_DONE`` every iteration and
never finishes has no idle streak to back it off. Backoff bounds a bot that has
nothing to do; nothing bounds a bot that always believes it has something. The
ledger is the only thing that does, which is why top-level bots need budgets
and not just children.

**Self-replication.** A child's allowance is *reserved from the parent's
remaining*, atomically, in the same transaction that creates it. So a bot
cannot conjure capacity by spawning — it can only divide what it already had,
and a runaway starves itself rather than starving you.

Denominated in iterations rather than dollars, on purpose. The deployment runs
on subscriptions, so there is no dollar signal at all: Omnigent's own budget
policies take ``max_cost_usd`` and never fire. What is actually scarce is turns.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from enum import Enum

from army.bots.store import BotStore, _statements
from army.store import ConcurrentTransition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_accounts (
    id        TEXT PRIMARY KEY,
    bot_id    TEXT NOT NULL,
    window    TEXT NOT NULL,
    allowance INTEGER NOT NULL,
    reserved  INTEGER NOT NULL DEFAULT 0,
    spent     INTEGER NOT NULL DEFAULT 0,
    resets_at INTEGER,
    version   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (bot_id, window)
);

-- Append-only. Recorded from the first iteration even before anything enforces
-- a limit, because usage history is the one thing that cannot be backfilled.
CREATE TABLE IF NOT EXISTS usage_ledger (
    id         TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    run_id     TEXT,
    delta      INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_account ON usage_ledger (account_id, at);
"""


class Window(str, Enum):
    """The period an allowance applies over."""

    DAY = "day"
    TOTAL = "total"


class BudgetExhausted(RuntimeError):
    """A bot has no allowance left. It pauses and asks for a refill.

    Never a silent stall: a bot that quietly stops is indistinguishable from
    one that is working, and the operator finds out days later.
    """


@dataclass(frozen=True)
class Account:
    """One bot's allowance over one window.

    :param id: Stable id.
    :param bot_id: Whose allowance.
    :param window: Over what period.
    :param allowance: Iterations granted.
    :param reserved: Held for children and for runs in flight.
    :param spent: Consumed.
    :param resets_at: When a daily window rolls over.
    :param version: CAS fencing token.
    """

    id: str
    bot_id: str
    window: Window
    allowance: int
    reserved: int = 0
    spent: int = 0
    resets_at: int | None = None
    version: int = 0

    @property
    def remaining(self) -> int:
        """What is still available to reserve or spend."""
        return max(0, self.allowance - self.reserved - self.spent)


class BudgetStore:
    """Allowances, reservations, and the append-only record of what went where.

    :param bots: The bot store, whose database and transactions this shares —
        a reservation must land in the same transaction as the run it pays for.
    """

    def __init__(self, bots: BotStore) -> None:
        self.bots = bots
        self.store = bots.store
        with self.store.atomic() as conn:
            for statement in _statements(_SCHEMA):
                conn.execute(statement)

    def grant(
        self,
        bot_id: str,
        allowance: int,
        *,
        window: Window = Window.TOTAL,
        resets_at: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Account:
        """
        Give a bot an allowance, or raise the one it has.

        :param bot_id: Whose allowance.
        :param allowance: Iterations.
        :param window: Over what period.
        :param resets_at: When a daily window rolls over.
        :param conn: Join an open transaction, or ``None``.
        :returns: The account.
        """
        with self.bots._tx(conn) as conn:
            conn.execute(
                "INSERT INTO budget_accounts (id, bot_id, window, allowance, resets_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(bot_id, window) DO UPDATE SET"
                " allowance = excluded.allowance, resets_at = excluded.resets_at,"
                " version = version + 1",
                (uuid.uuid4().hex, bot_id, window.value, allowance, resets_at),
            )
            # Re-read on the same connection. Opening a second one here would
            # take BEGIN IMMEDIATE against a lock this call may already hold,
            # and deadlock against itself until the busy timeout.
            account = self.account(bot_id, window=window, conn=conn)
        assert account is not None
        return account

    def account(
        self,
        bot_id: str,
        *,
        window: Window = Window.TOTAL,
        conn: sqlite3.Connection | None = None,
    ) -> Account | None:
        """
        Read one allowance.

        :param bot_id: Whose.
        :param window: Which period.
        :param conn: Join an open transaction, or ``None``.
        :returns: The account, or ``None`` when the bot has no budget — which
            means unlimited, and is the right default for a top-level bot an
            operator has not thought about yet.
        """
        with self.bots._tx(conn) as conn:
            row = conn.execute(
                "SELECT * FROM budget_accounts WHERE bot_id = ? AND window = ?",
                (bot_id, window.value),
            ).fetchone()
        return _row_to_account(row) if row is not None else None

    def charge(
        self,
        bot_id: str,
        *,
        run_id: str,
        now: int,
        amount: int = 1,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """
        Spend one iteration's allowance, in the transaction that opens the run.

        Recorded in the ledger whether or not an allowance exists. Usage
        history cannot be backfilled, and an operator deciding what a budget
        *should* be needs to see what it has actually been.

        :param bot_id: Whose allowance.
        :param run_id: The iteration being paid for.
        :param now: Epoch seconds.
        :param amount: Iterations.
        :param conn: Join the run's transaction, or ``None``.
        :raises BudgetExhausted: If there is an allowance and it is used up.
        """
        with self.bots._tx(conn) as conn:
            account = self.account(bot_id, conn=conn)
            if account is None:
                # No budget configured. Record the usage anyway, under a
                # synthetic account, so the history exists when one is.
                self._record(conn, f"unbudgeted:{bot_id}", run_id, amount, "run", now)
                return
            if account.remaining < amount:
                raise BudgetExhausted(
                    f"bot {bot_id} has {account.remaining} of {account.allowance} iterations "
                    f"left in its {account.window.value} budget; refill it or raise the allowance"
                )
            cursor = conn.execute(
                "UPDATE budget_accounts SET spent = spent + ?, version = version + 1"
                " WHERE id = ? AND version = ?",
                (amount, account.id, account.version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentTransition(
                    f"budget for {bot_id} changed while charging it; re-read and decide again"
                )
            self._record(conn, account.id, run_id, amount, "run", now)

    def carve(
        self,
        parent_bot_id: str,
        child_bot_id: str,
        allowance: int,
        *,
        now: int,
        conn: sqlite3.Connection | None = None,
    ) -> Account:
        """
        Move allowance from a parent to a new child, atomically.

        This is what makes self-replication starve itself. The parent's
        remaining is decremented under a compare-and-swap in the same
        transaction that grants the child, so two child approvals racing the
        same parent capacity cannot both succeed.

        :param parent_bot_id: Who is paying.
        :param child_bot_id: Who is being funded.
        :param allowance: Iterations to move.
        :param now: Epoch seconds.
        :param conn: Join the spawn's transaction, or ``None``.
        :returns: The child's account.
        :raises BudgetExhausted: If the parent cannot afford it.
        """
        if allowance <= 0:
            raise ValueError("a child needs a positive allowance")
        with self.bots._tx(conn) as conn:
            parent = self.account(parent_bot_id, conn=conn)
            if parent is None:
                raise BudgetExhausted(
                    f"parent {parent_bot_id} has no budget to carve from. "
                    "Give it one before it may fund a child — an unbudgeted parent "
                    "would let the fleet grow without limit."
                )
            if parent.remaining < allowance:
                raise BudgetExhausted(
                    f"parent {parent_bot_id} has {parent.remaining} iterations left "
                    f"and the child asks for {allowance}"
                )
            cursor = conn.execute(
                "UPDATE budget_accounts SET reserved = reserved + ?, version = version + 1"
                " WHERE id = ? AND version = ?",
                (allowance, parent.id, parent.version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentTransition(
                    f"budget for {parent_bot_id} changed while carving a child's share"
                )
            self._record(conn, parent.id, None, allowance, f"carved for {child_bot_id}", now)
            child_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO budget_accounts (id, bot_id, window, allowance)"
                " VALUES (?,?,?,?) ON CONFLICT(bot_id, window) DO UPDATE SET"
                " allowance = allowance + excluded.allowance, version = version + 1",
                (child_id, child_bot_id, Window.TOTAL.value, allowance),
            )
            self._record(conn, child_id, None, allowance, f"carved from {parent_bot_id}", now)
            # Same connection, same reason as `grant`.
            account = self.account(child_bot_id, conn=conn)
        assert account is not None
        return account

    def release(
        self,
        parent_bot_id: str,
        allowance: int,
        *,
        now: int,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """
        Return a retired child's unused reservation to its parent.

        Otherwise a fleet that churns children leaks capacity: every retirement
        would permanently shrink what the parent may do.

        :param parent_bot_id: Who gets it back.
        :param allowance: Iterations.
        :param now: Epoch seconds.
        :param conn: Join an open transaction, or ``None``.
        """
        with self.bots._tx(conn) as conn:
            account = self.account(parent_bot_id, conn=conn)
            if account is None:
                return
            conn.execute(
                "UPDATE budget_accounts SET reserved = MAX(0, reserved - ?),"
                " version = version + 1 WHERE id = ?",
                (allowance, account.id),
            )
            self._record(conn, account.id, None, -allowance, "child retired", now)

    def usage(self, bot_id: str, *, limit: int = 50) -> list[dict[str, object]]:
        """
        What a bot has spent, newest first.

        :param bot_id: Whose.
        :param limit: How many entries.
        :returns: Ledger rows.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT l.* FROM usage_ledger l LEFT JOIN budget_accounts a"
                " ON a.id = l.account_id WHERE a.bot_id = ? OR l.account_id = ?"
                " ORDER BY l.at DESC, l.id DESC LIMIT ?",
                (bot_id, f"unbudgeted:{bot_id}", limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def _record(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        run_id: str | None,
        delta: int,
        reason: str,
        at: int,
    ) -> None:
        """Append to the ledger. Never updated, never deleted."""
        conn.execute(
            "INSERT INTO usage_ledger (id, account_id, run_id, delta, reason, at)"
            " VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, account_id, run_id, delta, reason, at),
        )


def _row_to_account(row: sqlite3.Row) -> Account:
    """Rebuild an :class:`Account` from its row."""
    return Account(
        id=row["id"],
        bot_id=row["bot_id"],
        window=Window(row["window"]),
        allowance=row["allowance"],
        reserved=row["reserved"],
        spent=row["spent"],
        resets_at=row["resets_at"],
        version=row["version"],
    )
