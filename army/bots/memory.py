"""What a fresh body is told, so a disposable body is not an amnesiac one.

A bot throws away its session transcript every iteration. That is the price of
owning no process between iterations, and it is worth paying — but only if
something assembles the continuity back. Without this module the design's own
subtitle is false: there is no memory, just a persona re-read from a row.

Four things go into a briefing, and the order is the argument:

1. **Who you are** — persona and mission, from the *pinned revision*, not the
   current row. A run executes the definition it started under.
2. **Where you got to** — the last iteration's outcome and what it said, so
   the bot does not redo what it just did.
3. **What was decided** — recent verdicts, because "the human said no to this
   last time" is the single most expensive thing to forget.
4. **What arrived** — unread mail, with the cursor that makes reading it
   idempotent.

Everything is bounded. An unbounded briefing grows until it is the whole
history, and then the context window truncates it from the top — silently
dropping the persona, which is the one part that must never be dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from army.bots.messages import Message, MessageKind, MessageStore
from army.bots.model import Bot
from army.bots.store import BotStore

#: Iterations of history a briefing carries. Enough to see a pattern — "this
#: is the third time the tests failed the same way" — without turning the
#: prompt into a transcript.
HISTORY_DEPTH = 5

#: Verdicts carried forward. Decisions are the expensive thing to forget, so
#: they get more room than ordinary chatter.
VERDICT_DEPTH = 5

#: Unread messages included inline. Beyond this the briefing says how many are
#: waiting and lets the bot read them itself, rather than pasting a backlog
#: into every prompt.
INBOX_PREVIEW = 10

#: Characters of any single remembered item. A run that returned a whole file
#: must not push the persona out of the window.
ITEM_LIMIT = 800


@dataclass
class Briefing:
    """Everything a fresh body is told before it starts.

    :param persona: The standing role, from the pinned revision.
    :param mission: The objective, from the pinned revision.
    :param revision: Which revision this run pins.
    :param history: Recent iterations, newest first.
    :param verdicts: Recent human decisions, newest first.
    :param inbox: Unread messages.
    :param inbox_waiting: How many are unread in total.
    :param notes: Durable learned notes the bot has written for itself.
    :param cursor: The channel sequence this briefing was assembled at, so the
        next one can resume rather than repeat.
    """

    persona: str
    mission: str
    revision: int | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    inbox: list[Message] = field(default_factory=list)
    inbox_waiting: int = 0
    notes: list[str] = field(default_factory=list)
    cursor: int = 0

    def render(self) -> str:
        """
        The briefing as prompt text.

        Persona first and unabridged. Everything after it is context that can
        be truncated without changing who the bot is; the persona cannot.

        :returns: Markdown for the body's first message.
        """
        parts = [self.persona.strip(), "", "## Your mission", self.mission.strip()]

        if self.notes:
            parts += ["", "## What you have learned"]
            parts += [f"- {note}" for note in self.notes]

        if self.history:
            parts += ["", "## Where you got to"]
            for entry in self.history:
                outcome = entry.get("outcome") or "unknown"
                reason = entry.get("terminal_reason") or ""
                parts.append(f"- {outcome}: {reason}".rstrip(": ").rstrip())

        if self.verdicts:
            parts += ["", "## What was decided"]
            parts += [f"- {verdict}" for verdict in self.verdicts]

        if self.inbox:
            parts += ["", "## Messages for you"]
            for message in self.inbox:
                parts.append(f"- **{message.author}**: {_clip(message.body)}")
            if self.inbox_waiting > len(self.inbox):
                parts.append(
                    f"- …and {self.inbox_waiting - len(self.inbox)} more waiting; "
                    "read the rest before acting."
                )

        return "\n".join(parts).strip() + "\n"


def assemble(
    bot: Bot,
    bots: BotStore,
    messages: MessageStore | None = None,
    *,
    now: int,
    revision_id: str | None = None,
) -> Briefing:
    """
    Gather what this iteration's body needs to know.

    :param bot: The bot about to run.
    :param bots: Where its revisions and runs live.
    :param messages: Its channel, or ``None`` when there is no bus yet.
    :param now: Epoch seconds.
    :param revision_id: The revision this run pins; defaults to the bot's
        current one.
    :returns: The briefing.
    """
    persona, mission, rev = _pinned_definition(bot, bots, revision_id or bot.current_revision_id)
    briefing = Briefing(persona=persona, mission=mission, revision=rev)

    briefing.history = [
        {
            "outcome": run["outcome"],
            "terminal_reason": _clip(run["terminal_reason"] or ""),
            "at": run["updated_at"],
        }
        for run in bots.runs_for(bot.id, limit=HISTORY_DEPTH + 1)
        # The run being assembled for is not history yet.
        if run["outcome"] is not None
    ][:HISTORY_DEPTH]

    if messages is not None:
        briefing.verdicts = _recent_verdicts(bot, messages)
        briefing.notes = _notes(bot, messages)
        pending = messages.lease(bot.address, now=now, limit=INBOX_PREVIEW)
        briefing.inbox = [delivery.message for delivery in pending]
        briefing.inbox_waiting = len(pending) + messages.pending_for(bot.address, now=now)
        channel = messages.channel(bot.id, after_seq=0, limit=1)
        briefing.cursor = max(
            [message.seq for message in briefing.inbox] + [channel[0].seq if channel else 0]
        )
    return briefing


def _pinned_definition(
    bot: Bot, bots: BotStore, revision_id: str | None
) -> tuple[str, str, int | None]:
    """
    Read persona and mission from the revision a run pins, not the live row.

    A run executes the definition it started under. Reading the current row
    instead means an edit made mid-iteration silently changes what the running
    body was told, and the history stops meaning what it says.

    :param bot: The bot.
    :param bots: The store.
    :param revision_id: The pinned revision, or ``None``.
    :returns: ``(persona, mission, rev)``.
    """
    if revision_id is None:
        return bot.persona, bot.mission, None
    revision = bots.revision(revision_id)
    if revision is None:
        return bot.persona, bot.mission, None
    definition = revision.definition
    return (
        str(definition.get("persona") or bot.persona),
        str(definition.get("mission") or bot.mission),
        revision.rev,
    )


def _recent_verdicts(bot: Bot, messages: MessageStore) -> list[str]:
    """
    The last few human decisions, newest first.

    :param bot: The bot.
    :param messages: Its channel.
    :returns: One line per decision.
    """
    recent = [
        message
        for message in messages.channel(bot.id, after_seq=0, limit=200)
        if message.kind is MessageKind.VERDICT
    ]
    return [_clip(message.body) for message in reversed(recent[-VERDICT_DEPTH:])]


def _notes(bot: Bot, messages: MessageStore) -> list[str]:
    """
    Durable notes the bot has written for itself.

    Carried as ``report`` messages tagged ``lesson``, so a bot records what it
    learned the same way it records anything else and there is no second store
    to keep in step.

    :param bot: The bot.
    :param messages: Its channel.
    :returns: The notes, oldest first.
    """
    return [
        _clip(message.body)
        for message in messages.channel(bot.id, after_seq=0, limit=200)
        if message.kind is MessageKind.REPORT and message.payload.get("kind") == "lesson"
    ]


def remember(
    bot: Bot,
    messages: MessageStore,
    note: str,
    *,
    now: int,
    run_id: str | None = None,
) -> Message:
    """
    Write something the bot should still know next time.

    Deliberately narrow: a note, not an instruction. A bot editing its own
    persona to widen its own powers is a gated action and does not go through
    here — that is a revision, visible in the history and requiring a human.

    :param bot: The bot.
    :param messages: Its channel.
    :param note: What it learned.
    :param now: Epoch seconds.
    :param run_id: The iteration that learned it.
    :returns: The recorded message.
    """
    return messages.post(
        bot.id,
        bot.address,
        MessageKind.REPORT,
        note,
        now=now,
        payload={"kind": "lesson"},
        run_id=run_id,
    )


def _clip(text: str) -> str:
    """
    Bound one remembered item so it cannot crowd out the persona.

    :param text: The item.
    :returns: It, truncated with a marker if it was too long.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= ITEM_LIMIT:
        return collapsed
    return collapsed[: ITEM_LIMIT - 1] + "…"
