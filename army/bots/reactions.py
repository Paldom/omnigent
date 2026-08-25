"""Acknowledging a bot without authorising it.

Approve-or-deny makes a bot a vending machine, and :meth:`BotsSite.say` fixed
half of that: you can now tell a bot something. But a sentence is a heavy
instrument for the commonest thing a person actually wants to do, which is
signal that they have *read* the thing — and in a fleet of ten bots posting
reports overnight, "I saw this" is most of the traffic.

So: reactions. Buzz's affordance, and it fits here for a reason that has
nothing to do with fashion — this codebase's central rule is that **discussion
must not authorise**. An approval binds a verdict to an action hash, a policy
version and a run version. Nothing else may stand in for it. A reaction is the
clearest possible statement of that boundary: it is visible, it reaches the
bot, and it carries no authority whatsoever.

## Why the vocabulary is fixed, and why there is no tick

Free-form emoji would make this a decoration. A fixed set means each mark has
one meaning a bot can act on and a person can rely on.

There is deliberately **no ✅ and no 👍-as-approval**. A checkmark beside a
pending question reads as "approved" to every human who has ever used chat
software, and the one thing this system must never do is let something that
looks like a verdict be mistaken for one. The available marks say *seen*,
*useful*, *unclear* and *concern* — none of which a reasonable person would
read as consent, and the concern mark points the other way entirely.

## Reaching the bot

A reaction nobody but the UI can see is a nicer way of doing nothing. These are
handed to the bot in its next brief, with the boundary stated in the same
breath: somebody read this, and that is not permission.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from army.bots.store import BotStore
from army.store import add_missing_columns

_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_reactions (
    message_id TEXT NOT NULL,
    bot_id     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    mark       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    seen_at    INTEGER,
    retracted_at INTEGER,
    PRIMARY KEY (message_id, actor, mark)
);
-- The channel read wants every reaction for one bot in one query, not one
-- query per message.
CREATE INDEX IF NOT EXISTS ix_reactions_bot ON message_reactions (bot_id, created_at);
"""

#: What a mark means, in the words the bot is given. The keys are the whole
#: vocabulary — anything else is refused rather than stored, so a mark always
#: means one thing.
#:
#: No tick and no thumbs-down: beside a pending question either would read as a
#: verdict, and a verdict has to be bound to an action hash to mean anything.
MARKS: dict[str, str] = {
    "seen": "a person has read this",
    "useful": "a person found this worth having",
    "unclear": "a person could not follow this",
    "concern": "a person is uneasy about this",
}


class ReactionRefused(RuntimeError):
    """A mark outside the vocabulary, or a message that is not this bot's."""


@dataclass(frozen=True)
class Reaction:
    """One mark, by one person, on one message.

    :param message_id: What was marked.
    :param bot_id: Whose channel it is in.
    :param actor: Who marked it.
    :param mark: A key of :data:`MARKS`.
    :param created_at: Epoch seconds.
    :param seen_at: When the bot was told, or ``None``.
    """

    message_id: str
    bot_id: str
    actor: str
    mark: str
    created_at: int
    seen_at: int | None
    #: When the person took this mark back, or ``None``. A retraction the bot
    #: was never told about is a deleted row; this is the other case.
    retracted_at: int | None = None


class ReactionStore:
    """Marks on messages, in the same database as everything else.

    :param bots: The bot store, whose connection policy this borrows.
    """

    def __init__(self, bots: BotStore) -> None:
        self.bots = bots
        self.store = bots.store
        with self.store.atomic() as conn:
            # Statement at a time, not `executescript`: that issues an implicit
            # COMMIT, which ends the transaction `atomic` is holding and leaves
            # the caller committing nothing.
            for statement in filter(None, (part.strip() for part in _SCHEMA.split(";"))):
                conn.execute(statement)
            add_missing_columns(conn, "message_reactions", (("retracted_at", "INTEGER"),))

    def toggle(self, message_id: str, actor: str, mark: str, *, now: int) -> bool:
        """
        Add a mark, or take it back if this actor already made it.

        Toggling rather than adding, because the mistake people make with
        reactions is the mis-click, and a mark you cannot remove is a statement
        you cannot retract.

        :param message_id: What is being marked.
        :param actor: Who is marking it.
        :param mark: A key of :data:`MARKS`.
        :param now: Epoch seconds.
        :returns: Whether the mark is now present.
        :raises ReactionRefused: On an unknown mark or message.
        """
        if mark not in MARKS:
            raise ReactionRefused(
                f"{mark!r} is not one of {', '.join(sorted(MARKS))} — "
                "a mark that means anything means nothing"
            )
        with self.store.atomic() as conn:
            row = conn.execute(
                "SELECT bot_id FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise ReactionRefused("no such message")
            existing = conn.execute(
                "SELECT seen_at, retracted_at FROM message_reactions"
                " WHERE message_id = ? AND actor = ? AND mark = ?",
                (message_id, actor, mark),
            ).fetchone()
            if existing is not None and existing["retracted_at"] is not None:
                # Marked again after a retraction. Live once more, and unseen,
                # so the bot is told about this too.
                conn.execute(
                    "UPDATE message_reactions SET retracted_at = NULL, seen_at = NULL,"
                    " created_at = ? WHERE message_id = ? AND actor = ? AND mark = ?",
                    (now, message_id, actor, mark),
                )
                return True
            if existing is not None:
                if existing["seen_at"] is None:
                    # Nobody was told, so there is nothing to take back.
                    conn.execute(
                        "DELETE FROM message_reactions"
                        " WHERE message_id = ? AND actor = ? AND mark = ?",
                        (message_id, actor, mark),
                    )
                else:
                    # The bot has already been briefed on this. Deleting the row
                    # would leave it believing something the UI no longer shows —
                    # a mark you cannot see and cannot argue with. Tombstoned
                    # instead, and the retraction is briefed like the mark was.
                    conn.execute(
                        "UPDATE message_reactions SET retracted_at = ?, seen_at = NULL"
                        " WHERE message_id = ? AND actor = ? AND mark = ?",
                        (now, message_id, actor, mark),
                    )
                return False
            conn.execute(
                "INSERT INTO message_reactions"
                " (message_id, bot_id, actor, mark, created_at, seen_at)"
                " VALUES (?,?,?,?,?,NULL)",
                (message_id, row["bot_id"], actor, mark, now),
            )
        return True

    def for_bot(self, bot_id: str) -> dict[str, list[Reaction]]:
        """
        Every mark in one bot's channel, keyed by message.

        One query rather than one per message: a channel is a page of rows and
        the roster renders several of them.

        :param bot_id: Whose channel.
        :returns: ``{message_id: [Reaction, ...]}``.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT * FROM message_reactions WHERE bot_id = ?"
                " AND retracted_at IS NULL ORDER BY created_at",
                (bot_id,),
            ).fetchall()
        found: dict[str, list[Reaction]] = {}
        for row in rows:
            found.setdefault(row["message_id"], []).append(_row(row))
        return found

    def take_unseen(self, bot_id: str, *, now: int) -> list[Reaction]:
        """
        Marks the bot has not been told about, claimed so it is told once.

        Marked seen here rather than after the bot replies, for the same reason
        :meth:`MessageStore.lease` acks early: a reaction redelivered into every
        subsequent brief reads to the body as the operator repeating themselves.

        :param bot_id: Whose channel.
        :param now: Epoch seconds.
        :returns: The marks, oldest first.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT * FROM message_reactions"
                " WHERE bot_id = ? AND seen_at IS NULL ORDER BY created_at",
                (bot_id,),
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE message_reactions SET seen_at = ?"
                    " WHERE bot_id = ? AND seen_at IS NULL",
                    (now, bot_id),
                )
        return [_row(row) for row in rows]


def briefing(reactions: list[Reaction], bodies: dict[str, str]) -> str:
    """
    What to tell a bot about the marks on its work, boundary included.

    The boundary is in the same paragraph as the news on purpose. A bot told
    "somebody marked your report useful" and left to draw its own conclusion
    will draw the wrong one on the iteration where it matters.

    :param reactions: Unseen marks.
    :param bodies: ``{message_id: message body}``, for quoting what was marked.
    :returns: A markdown section, or ``""`` when there is nothing to say.
    """
    if not reactions:
        return ""
    lines = []
    for reaction in reactions:
        quoted = " ".join(bodies.get(reaction.message_id, "").split())[:70]
        if reaction.retracted_at is not None:
            # Told once, taken back. Silence would leave the bot acting on a
            # mark the person has withdrawn and the UI no longer shows.
            lines.append(
                f"- **{reaction.mark} — withdrawn.** They have taken this back: “{quoted}”"
            )
        else:
            lines.append(f"- **{reaction.mark}** — {MARKS[reaction.mark]}: “{quoted}”")
    return (
        "## Somebody read your work\n\n"
        + "\n".join(lines)
        + "\n\nThis is acknowledgement, **not** permission. Nothing here approves "
        "anything, changes what you are allowed to do, or answers a question you "
        "have open — an approval arrives as a decision on a specific action and "
        "looks nothing like this. Treat `concern` and `unclear` as worth "
        "addressing in what you do next; treat none of them as a yes."
    )


def _row(row: sqlite3.Row) -> Reaction:
    """Rebuild a mark from its row."""
    return Reaction(
        message_id=row["message_id"],
        bot_id=row["bot_id"],
        actor=row["actor"],
        mark=row["mark"],
        created_at=int(row["created_at"]),
        seen_at=None if row["seen_at"] is None else int(row["seen_at"]),
        retracted_at=None if row["retracted_at"] is None else int(row["retracted_at"]),
    )
