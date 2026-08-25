"""The one input a bot both writes and reads."""

from __future__ import annotations

from army.bots.memory import NOTE_DEPTH, assemble, remember
from army.bots.messages import MessageStore
from army.bots.store import BotStore
from tests.army.bots.conftest import activate, make_bot

NOW = 1_700_000_000


def test_a_correction_outranks_what_it_corrects(bots: BotStore) -> None:
    """A note is the one briefing input a bot both writes and reads.

    It is capped in the briefing rather than deleted from the channel: a
    time-to-live on the table evicts a load-bearing note as readily as a
    poisonous one, and the bot has no way to tell which it lost.
    """
    messages = MessageStore(bots)
    bot = activate(bots, make_bot("scout"), now=NOW)
    remember(bot, messages, "the status page moved to /health", now=NOW - 86_400)
    remember(bot, messages, "correction: /health was a redirect", now=NOW)

    notes = assemble(bot, bots, messages, now=NOW).notes

    assert "correction" in notes[0], "newest first, so a correction is read first"


def test_notes_are_capped_newest_first(bots: BotStore) -> None:
    """An unbounded self-written channel is a bot arguing with its own past."""
    messages = MessageStore(bots)
    bot = activate(bots, make_bot("scout"), now=NOW)
    for n in range(NOTE_DEPTH + 5):
        remember(bot, messages, f"note {n}", now=NOW - (NOTE_DEPTH + 5 - n))

    notes = assemble(bot, bots, messages, now=NOW).notes

    assert len(notes) == NOTE_DEPTH
    assert f"note {NOTE_DEPTH + 4}" in notes[0], "newest first"
    assert not any("note 0" in note for note in notes), "oldest dropped"


def test_a_note_carries_its_age(bots: BotStore) -> None:
    """The bot has no other way to see that a note has gone stale."""
    messages = MessageStore(bots)
    bot = activate(bots, make_bot("scout"), now=NOW)
    remember(bot, messages, "the fee is 0.40%", now=NOW - 3 * 86_400)

    assert assemble(bot, bots, messages, now=NOW).notes[0].startswith("3d ago:")


def test_a_verdict_carries_the_question_it_answered(bots: BotStore) -> None:
    """ "Denied" alone is a decision a bot cannot scope.

    The approval bound the verdict to an exact action; reaching the next body
    as bare prose throws that away one layer later, and the bot reads it as
    covering today's different action or as covering nothing.
    """
    from army.bots.messages import MessageKind

    messages = MessageStore(bots)
    bot = activate(bots, make_bot("scout"), now=NOW)
    # Production threads both on the run id — the approval request and the ask
    # are opened with `thread_id=run.id`, and the verdict answers on the same.
    thread = "r" * 32
    messages.post(
        bot.id,
        "system",
        MessageKind.ASK,
        "Open a pull request against main?",
        now=NOW,
        thread_id=thread,
    )
    messages.post(
        bot.id, "human:dpal", MessageKind.VERDICT, "Denied", now=NOW + 1, thread_id=thread
    )

    verdicts = assemble(bot, bots, messages, now=NOW + 2).verdicts

    assert "Denied" in verdicts[0]
    assert "pull request" in verdicts[0], "the bot must see what was denied"


def test_a_busy_channel_does_not_bury_the_latest_decisions(bots: BotStore) -> None:
    """`channel(limit=n)` is the *oldest* n — right for paging, wrong for "lately".

    Notes and verdicts both used it, so a bot whose channel had filled with
    wheel and schedule events was briefed on its first decisions forever and
    never its last ones. Two hundred events is a fortnight, not a lifetime.
    """
    from army.bots.messages import MessageKind

    messages = MessageStore(bots)
    bot = activate(bots, make_bot("scout"), now=NOW)
    thread = "r" * 32
    messages.post(
        bot.id, "system", MessageKind.ASK, "Ship the old thing?", now=NOW, thread_id=thread
    )
    messages.post(
        bot.id, "human:dpal", MessageKind.VERDICT, "Denied", now=NOW + 1, thread_id=thread
    )
    for n in range(260):
        messages.post(bot.id, "human:channel", MessageKind.EVENT, f"wheel {n}", now=NOW + 2 + n)
    later = "s" * 32
    messages.post(
        bot.id, "system", MessageKind.ASK, "Ship the new thing?", now=NOW + 400, thread_id=later
    )
    messages.post(
        bot.id, "human:dpal", MessageKind.VERDICT, "Approved", now=NOW + 401, thread_id=later
    )

    verdicts = assemble(bot, bots, messages, now=NOW + 500).verdicts

    assert any("new thing" in line for line in verdicts), "the latest decision must be briefed"
