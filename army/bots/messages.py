"""One substrate for everything addressed at a bot: mail, asks, verdicts, reports.

Human-to-bot and bot-to-bot are the same problem — ordered, durable delivery to
a named party — so they get one table rather than two delivery guarantees to
get right. ACP is deliberately not used for this: it is a transport for driving
a harness, with no durable queue, no cursor and no replay, and running it
between two bots on one Mac would be protocol for protocol's sake.

Two properties carry the weight.

**The ack is written after the receiving bot commits its transition, not on
receipt.** A crash mid-processing therefore redelivers rather than loses, which
makes delivery at-least-once and puts the burden of idempotency on the
``message_id`` — where it can actually be discharged.

**``seq`` comes from a counter row bumped inside the insert transaction.**
``MAX(seq) + 1`` read on its own connection races two writers into an
``IntegrityError`` on the uniqueness constraint, and the loser's message is
gone.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from army.bots.model import dumps
from army.bots.store import BotStore, _statements
from army.state import loads

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    bot_id     TEXT NOT NULL,
    thread_id  TEXT,
    run_id     TEXT,
    seq        INTEGER NOT NULL,
    author     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    body       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    command_id TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (bot_id, seq)
);
-- The channel read: everything in one bot's channel after a cursor.
CREATE INDEX IF NOT EXISTS ix_messages_channel ON messages (bot_id, seq);
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages (bot_id, thread_id, seq);

-- The per-bot cursor, bumped inside the insert's own transaction. Without it
-- two writers both read MAX(seq) and one loses its message to the unique index.
CREATE TABLE IF NOT EXISTS message_counters (
    bot_id   TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS message_deliveries (
    message_id   TEXT NOT NULL,
    recipient    TEXT NOT NULL,
    state        TEXT NOT NULL,
    available_at INTEGER NOT NULL,
    lease_until  INTEGER,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    PRIMARY KEY (message_id, recipient)
);
-- The delivery scan: what is owed to this recipient, oldest first.
CREATE INDEX IF NOT EXISTS ix_deliveries_pending
    ON message_deliveries (recipient, state, available_at);
"""

#: How long a leased message stays claimed before another attempt may take it.
#: Long enough to outlast an iteration, short enough that a crashed worker's
#: mail is not stuck until morning.
LEASE_SECONDS = 900

#: Attempts before a delivery is parked as ``DEAD``. A message that has failed
#: this many times is failing for a reason redelivery will not fix, and
#: redelivering it forever starves everything behind it.
MAX_ATTEMPTS = 5


class MessageKind(str, Enum):
    """What a row in a channel is."""

    HUMAN_MSG = "human_msg"
    BOT_MSG = "bot_msg"
    BOT_TO_BOT = "bot_to_bot"
    ASK = "ask"
    VERDICT = "verdict"
    REPORT = "report"
    EVENT = "event"


class DeliveryState(str, Enum):
    """Where one recipient's copy of a message has got to."""

    QUEUED = "queued"
    LEASED = "leased"
    ACKED = "acked"
    DEAD = "dead"


@dataclass(frozen=True)
class Message:
    """One row in a bot's channel.

    :param id: Stable message id, and the idempotency key a receiver dedupes on.
    :param bot_id: Whose channel this belongs to.
    :param seq: Monotonic within that channel — the cursor a reader stores.
    :param author: ``"human:<id>"`` | ``"bot:<id>"`` | ``"system"``.
    :param kind: What sort of message.
    :param body: Markdown for a person to read.
    :param payload: Structured content for a machine to act on.
    :param thread_id: The run or approval this belongs under, or ``None`` for
        the channel root.
    :param run_id: Which iteration produced it.
    :param command_id: Set on a verdict, linking it to the durable command.
    :param created_at: Epoch seconds.
    """

    id: str
    bot_id: str
    seq: int
    author: str
    kind: MessageKind
    body: str
    payload: dict[str, Any] = field(default_factory=dict)
    thread_id: str | None = None
    run_id: str | None = None
    command_id: str | None = None
    created_at: int = 0


@dataclass(frozen=True)
class Delivery:
    """One recipient's copy of a message, and how it is going.

    :param message: The message itself.
    :param recipient: Who owes an ack.
    :param state: Where it has got to.
    :param attempts: How many times it has been leased.
    """

    message: Message
    recipient: str
    state: DeliveryState
    attempts: int


class MessageStore:
    """The channel, and the delivery state that hangs off it.

    :param bots: The bot store, whose database and transaction policy this
        shares — an ack has to land in the same transaction as the state change
        it acknowledges.
    """

    def __init__(self, bots: BotStore) -> None:
        self.bots = bots
        self.store = bots.store
        with self.store.atomic() as conn:
            for statement in _statements(_SCHEMA):
                conn.execute(statement)

    # ── writing ───────────────────────────────────────────────────

    def post(
        self,
        bot_id: str,
        author: str,
        kind: MessageKind,
        body: str,
        *,
        now: int,
        payload: dict[str, Any] | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
        command_id: str | None = None,
        deliver_to: list[str] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Message:
        """
        Append to a bot's channel, and owe it to whoever must act on it.

        The sequence number and the row are written together. Reading
        ``MAX(seq)`` first and inserting second is the version of this that
        loses messages under two writers.

        :param bot_id: Whose channel.
        :param author: Who is speaking.
        :param kind: What sort of message.
        :param body: Markdown for a person.
        :param now: Epoch seconds.
        :param payload: Structured content for a machine.
        :param thread_id: The run or approval it belongs under.
        :param run_id: The iteration that produced it.
        :param command_id: The verdict's durable command.
        :param deliver_to: Recipients that owe an ack. ``None`` means nobody
            has to act — a report or an event that is only there to be read.
        :param conn: Join an open transaction, or ``None``.
        :returns: The message, with its sequence number.
        """
        with self.bots._tx(conn) as conn:
            row = conn.execute(
                "INSERT INTO message_counters (bot_id, next_seq) VALUES (?, 1)"
                " ON CONFLICT(bot_id) DO UPDATE SET next_seq = next_seq + 1"
                " RETURNING next_seq",
                (bot_id,),
            ).fetchone()
            message = Message(
                id=uuid.uuid4().hex,
                bot_id=bot_id,
                seq=int(row["next_seq"]),
                author=author,
                kind=kind,
                body=body,
                payload=payload or {},
                thread_id=thread_id,
                run_id=run_id,
                command_id=command_id,
                created_at=now,
            )
            conn.execute(
                "INSERT INTO messages (id, bot_id, thread_id, run_id, seq, author, kind,"
                " body, payload, command_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message.id,
                    message.bot_id,
                    message.thread_id,
                    message.run_id,
                    message.seq,
                    message.author,
                    message.kind.value,
                    message.body,
                    dumps(message.payload),
                    message.command_id,
                    message.created_at,
                ),
            )
            for recipient in deliver_to or []:
                conn.execute(
                    "INSERT OR IGNORE INTO message_deliveries"
                    " (message_id, recipient, state, available_at, attempts)"
                    " VALUES (?,?,?,?,0)",
                    (message.id, recipient, DeliveryState.QUEUED.value, now),
                )
        return message

    def lease(
        self,
        recipient: str,
        *,
        now: int,
        limit: int = 20,
        conn: sqlite3.Connection | None = None,
    ) -> list[Delivery]:
        """
        Claim what a recipient owes an ack on, without acknowledging it.

        Leasing rather than reading: a message handed to a body that then dies
        must come back, and a plain read gives nothing to come back *from*. The
        lease expires, so a crashed worker's mail is redelivered rather than
        held forever.

        :param recipient: ``"bot:<id>"`` or ``"human:<id>"``.
        :param now: Epoch seconds.
        :param limit: Most to claim at once.
        :param conn: Join an open transaction, or ``None``.
        :returns: The claimed deliveries, oldest first.
        """
        with self.bots._tx(conn) as conn:
            rows = conn.execute(
                "SELECT d.message_id, d.recipient, d.state, d.attempts, m.*"
                " FROM message_deliveries d JOIN messages m ON m.id = d.message_id"
                " WHERE d.recipient = ? AND d.available_at <= ?"
                "   AND (d.state = ? OR (d.state = ? AND d.lease_until <= ?))"
                " ORDER BY m.created_at, m.seq LIMIT ?",
                (
                    recipient,
                    now,
                    DeliveryState.QUEUED.value,
                    DeliveryState.LEASED.value,
                    now,
                    limit,
                ),
            ).fetchall()
            claimed = []
            for row in rows:
                attempts = int(row["attempts"]) + 1
                if attempts > MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE message_deliveries SET state = ?, last_error = ?"
                        " WHERE message_id = ? AND recipient = ?",
                        (
                            DeliveryState.DEAD.value,
                            f"gave up after {MAX_ATTEMPTS} attempts",
                            row["message_id"],
                            recipient,
                        ),
                    )
                    continue
                conn.execute(
                    "UPDATE message_deliveries SET state = ?, lease_until = ?, attempts = ?"
                    " WHERE message_id = ? AND recipient = ?",
                    (
                        DeliveryState.LEASED.value,
                        now + LEASE_SECONDS,
                        attempts,
                        row["message_id"],
                        recipient,
                    ),
                )
                claimed.append(
                    Delivery(
                        message=_row_to_message(row),
                        recipient=recipient,
                        state=DeliveryState.LEASED,
                        attempts=attempts,
                    )
                )
        return claimed

    def ack(
        self,
        message_id: str,
        recipient: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """
        Record that a recipient has finished acting on a message.

        Pass the *conn* the receiver's own state change is being written on.
        That is the entire delivery guarantee: the ack and the effect land
        together, so a crash between them is impossible rather than merely
        unlikely, and a crash before both redelivers.

        :param message_id: The message.
        :param recipient: Who is acknowledging.
        :param conn: The transaction the receiver's state change is in.
        """
        with self.bots._tx(conn) as conn:
            conn.execute(
                "UPDATE message_deliveries SET state = ?, lease_until = NULL"
                " WHERE message_id = ? AND recipient = ?",
                (DeliveryState.ACKED.value, message_id, recipient),
            )

    def fail(
        self,
        message_id: str,
        recipient: str,
        error: str,
        *,
        now: int,
        retry_in_s: int = 60,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """
        Hand a message back after failing to act on it.

        :param message_id: The message.
        :param recipient: Who failed.
        :param error: What went wrong, for the operator.
        :param now: Epoch seconds.
        :param retry_in_s: How long before it may be claimed again.
        :param conn: Join an open transaction, or ``None``.
        """
        with self.bots._tx(conn) as conn:
            conn.execute(
                "UPDATE message_deliveries SET state = ?, lease_until = NULL,"
                " available_at = ?, last_error = ? WHERE message_id = ? AND recipient = ?",
                (
                    DeliveryState.QUEUED.value,
                    now + retry_in_s,
                    error[:500],
                    message_id,
                    recipient,
                ),
            )

    # ── reading ───────────────────────────────────────────────────

    def channel(self, bot_id: str, *, after_seq: int = 0, limit: int = 100) -> list[Message]:
        """
        Read a bot's channel after a cursor.

        Replayable across a restart in a way an ``asyncio.Queue`` is not: a
        reader stores a ``seq`` and asks for everything past it, and the answer
        is the same whether the process has been up for a second or a week.

        :param bot_id: Whose channel.
        :param after_seq: The cursor.
        :param limit: Most to return.
        :returns: Messages in order.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE bot_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (bot_id, after_seq, limit),
            ).fetchall()
        return [_row_to_message(row) for row in rows]

    def latest(
        self, bot_id: str, *, kinds: tuple[MessageKind, ...] | None = None, limit: int = 20
    ) -> list[Message]:
        """
        The most recent messages in a bot's channel, newest first.

        :meth:`channel` is ``ORDER BY seq LIMIT n`` — the *oldest* n after a
        cursor, which is right for paging a conversation forward and wrong for
        "what happened lately". Two callers wanted the latter and used the
        former: the briefing's notes and its verdicts both read a bot's
        earliest two hundred messages, so a bot that had said much of anything
        was briefed on its first decisions forever and never its last ones. A
        channel fills with wheel and schedule events, so two hundred is a
        fortnight, not a lifetime.

        :param bot_id: Whose channel.
        :param kinds: Only these kinds, or ``None`` for all.
        :param limit: Most to return.
        :returns: Messages, newest first.
        """
        query = "SELECT * FROM messages WHERE bot_id = ?"
        params: list[Any] = [bot_id]
        if kinds:
            query += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += [kind.value for kind in kinds]
        query += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        with self.store.atomic() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_message(row) for row in rows]

    def thread(self, bot_id: str, thread_id: str, *, limit: int = 100) -> list[Message]:
        """
        Read one thread — an iteration, or an approval and its answer.

        :param bot_id: Whose channel.
        :param thread_id: The thread.
        :param limit: Most to return.
        :returns: Messages in order.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE bot_id = ? AND thread_id = ? ORDER BY seq LIMIT ?",
                (bot_id, thread_id, limit),
            ).fetchall()
        return [_row_to_message(row) for row in rows]

    def pending_for(self, recipient: str, *, now: int) -> int:
        """
        How many messages a recipient still owes an ack on.

        The cheap answer the tick needs: a bot with mail is worth waking, and
        this costs one indexed count rather than a read of the messages
        themselves.

        :param recipient: ``"bot:<id>"`` or ``"human:<id>"``.
        :param now: Epoch seconds.
        :returns: The count.
        """
        with self.store.atomic() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS pending FROM message_deliveries"
                " WHERE recipient = ? AND available_at <= ?"
                "   AND (state = ? OR (state = ? AND lease_until <= ?))",
                (
                    recipient,
                    now,
                    DeliveryState.QUEUED.value,
                    DeliveryState.LEASED.value,
                    now,
                ),
            ).fetchone()
        return int(row["pending"])

    def waiting_recipients(self, *, now: int) -> dict[str, int]:
        """
        Every recipient with mail, and how much — one query for the whole fleet.

        The wake scan asks this once per tick rather than once per bot, because
        a per-bot query is the thing that makes a forty-bot roster slow exactly
        when it is busy.

        :param now: Epoch seconds.
        :returns: ``{recipient: count}``.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT recipient, COUNT(*) AS pending FROM message_deliveries"
                " WHERE available_at <= ? AND (state = ? OR (state = ? AND lease_until <= ?))"
                " GROUP BY recipient",
                (now, DeliveryState.QUEUED.value, DeliveryState.LEASED.value, now),
            ).fetchall()
        return {row["recipient"]: int(row["pending"]) for row in rows}

    def waiting_from_humans(self, *, now: int) -> dict[str, int]:
        """
        The same scan, restricted to mail a person wrote.

        The wake model deliberately refuses to pull a *scheduled* bot forward
        when mail arrives: two bots that talk to each other would spin without
        anybody involved. A person is the exception to that argument rather
        than a hole in it — nobody types fast enough to spin a loop, and a bot
        that ignores you for fourteen minutes because its interval says so is
        not a colleague.

        Joined to ``messages`` so the exception cannot be claimed by a bot
        addressing itself as a human: the author is what decides, and only the
        channel writes that.

        :param now: Epoch seconds.
        :returns: ``{recipient: count}`` for human-authored mail only.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT d.recipient AS recipient, COUNT(*) AS pending"
                " FROM message_deliveries d JOIN messages m ON m.id = d.message_id"
                " WHERE d.available_at <= ?"
                " AND (d.state = ? OR (d.state = ? AND d.lease_until <= ?))"
                " AND m.kind = ?"
                " GROUP BY d.recipient",
                (
                    now,
                    DeliveryState.QUEUED.value,
                    DeliveryState.LEASED.value,
                    now,
                    MessageKind.HUMAN_MSG.value,
                ),
            ).fetchall()
        return {row["recipient"]: int(row["pending"]) for row in rows}

    def dead_letters(self) -> list[tuple[str, str, str]]:
        """
        Deliveries that were given up on, so they are visible rather than lost.

        :returns: ``(message_id, recipient, last_error)`` for each.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT message_id, recipient, last_error FROM message_deliveries WHERE state = ?",
                (DeliveryState.DEAD.value,),
            ).fetchall()
        return [(row["message_id"], row["recipient"], row["last_error"] or "") for row in rows]


def _row_to_message(row: sqlite3.Row) -> Message:
    """Rebuild a :class:`Message` from a row of ``messages``."""
    return Message(
        id=row["id"],
        bot_id=row["bot_id"],
        seq=row["seq"],
        author=row["author"],
        kind=MessageKind(row["kind"]),
        body=row["body"],
        payload=loads(row["payload"], {}),
        thread_id=row["thread_id"],
        run_id=row["run_id"],
        command_id=row["command_id"],
        created_at=row["created_at"],
    )
