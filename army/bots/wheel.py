"""Who is driving a bot's browser: the bot, or a person.

OpenBot's rule is the one worth copying verbatim — *while a person is driving,
bot actions are refused rather than queued*. Queuing is the tempting version
and it is wrong: a queued click lands after the human has navigated away, on a
page that is no longer the one it was reasoned about. A refusal with a reason
is something an agent can handle; a delayed click is something nobody can.

## Why a lease and not a flag

A person takes the wheel, then closes the laptop. A flag would hold that bot's
browser forever and the failure would look like the bot being broken. The lease
expires, and taking it again is one click — the same shape
``MessageStore.lease`` already uses for mail, for the same reason.

## What this is not

It is not a permission system. A bot that is *refused* the browser can still
read files, run its workload, and finish its iteration; the wheel governs one
device. Anything about what a bot may do at all belongs in the approval
machinery, which binds a verdict to an action hash — a lock keyed only by time
would be a much weaker thing wearing the same name.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from army.bots.store import BotStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_wheel (
    bot_id     TEXT PRIMARY KEY,
    driver     TEXT NOT NULL,
    reason     TEXT,
    taken_at   INTEGER NOT NULL,
    held_until INTEGER NOT NULL
);
"""

#: How long one grab of the wheel lasts before it falls back to the bot.
#: Long enough to log in to something and read a page; short enough that a
#: closed laptop is not a stuck bot.
DEFAULT_LEASE_S = 900

#: What a refused bot is told. Written for an agent rather than a log: it says
#: what happened, that waiting is correct, and that retrying is not.
REFUSAL = (
    "A person has taken the wheel of this browser. Your action was refused, "
    "not queued — the page may be somewhere else by the time they hand it "
    "back. Wait, say in your reply that you were interrupted, and do not "
    "retry in a loop."
)


@dataclass(frozen=True)
class Wheel:
    """Who holds a bot's browser, and until when.

    :param bot_id: Whose browser.
    :param driver: ``"human:<id>"``. A bot is never recorded as the driver —
        the bot driving is the absence of a row, so a lost write fails towards
        the bot working rather than towards it being locked out.
    :param reason: Why they took it, for the channel.
    :param taken_at: Epoch seconds.
    :param held_until: When it falls back to the bot.
    """

    bot_id: str
    driver: str
    reason: str | None
    taken_at: int
    held_until: int


class WheelStore:
    """Who is driving, in the same database as everything else.

    :param bots: The bot store, whose connection policy this borrows.
    """

    def __init__(self, bots: BotStore) -> None:
        self.bots = bots
        self.store = bots.store
        with self.store.atomic() as conn:
            conn.execute(_SCHEMA.strip())

    def take(
        self,
        bot_id: str,
        driver: str,
        *,
        now: int,
        reason: str | None = None,
        lease_s: int = DEFAULT_LEASE_S,
    ) -> Wheel:
        """
        Take the wheel, or extend a hold you already have.

        Taking one somebody else holds is allowed and deliberate: two people
        watching one bot is a normal Tuesday, and refusing the second would
        mean the first has to be found before anything can happen. The channel
        records both, which is the control that matters.

        :param bot_id: Whose browser.
        :param driver: Who is taking it.
        :param now: Epoch seconds.
        :param reason: Why, for the channel.
        :param lease_s: How long the hold lasts.
        :returns: The hold.
        """
        wheel = Wheel(bot_id, driver, reason, now, now + lease_s)
        with self.store.atomic() as conn:
            conn.execute(
                "INSERT INTO browser_wheel (bot_id, driver, reason, taken_at, held_until)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(bot_id) DO UPDATE SET driver = excluded.driver,"
                " reason = excluded.reason, taken_at = excluded.taken_at,"
                " held_until = excluded.held_until",
                (bot_id, driver, reason, now, wheel.held_until),
            )
        return wheel

    def release(self, bot_id: str, *, conn: sqlite3.Connection | None = None) -> None:
        """
        Hand the browser back to the bot.

        Deleting rather than marking released: the bot driving is the absence
        of a row, so there is one state to read and no way to be half-released.

        :param bot_id: Whose browser.
        :param conn: Join an open transaction, or ``None``.
        """
        with self.bots._tx(conn) as conn:
            conn.execute("DELETE FROM browser_wheel WHERE bot_id = ?", (bot_id,))

    def held_by(self, bot_id: str, *, now: int) -> Wheel | None:
        """
        Who is driving, or ``None`` when the bot is.

        An expired hold reads as ``None`` rather than being cleaned up first,
        so a reader never has to write and the answer cannot depend on whether
        anybody has swept recently.

        :param bot_id: Whose browser.
        :param now: Epoch seconds.
        :returns: The live hold, or ``None``.
        """
        with self.store.atomic() as conn:
            row = conn.execute(
                "SELECT * FROM browser_wheel WHERE bot_id = ? AND held_until > ?",
                (bot_id, now),
            ).fetchone()
        return _row(row) if row is not None else None

    def all_held(self, *, now: int) -> dict[str, Wheel]:
        """
        Every live hold, for the roster — one query, not one per bot.

        :param now: Epoch seconds.
        :returns: ``{bot_id: Wheel}``.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT * FROM browser_wheel WHERE held_until > ?", (now,)
            ).fetchall()
        return {row["bot_id"]: _row(row) for row in rows}


def _row(row: sqlite3.Row) -> Wheel:
    """Rebuild a hold from its row."""
    return Wheel(
        bot_id=row["bot_id"],
        driver=row["driver"],
        reason=row["reason"],
        taken_at=int(row["taken_at"]),
        held_until=int(row["held_until"]),
    )
