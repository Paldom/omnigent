"""Acknowledging a bot without authorising it.

The whole point is the boundary. This system binds an approval to an action
hash, a policy version and a run version precisely so that nothing else can
stand in for consent — so a new way for a person to respond to a bot is only
safe if it cannot be mistaken for one, by the person or by the bot.
"""

from __future__ import annotations

import pytest

from army.bots.approvals import ApprovalStore
from army.bots.messages import MessageKind, MessageStore
from army.bots.model import WakeKind, WakePolicy
from army.bots.reactions import MARKS, ReactionRefused, ReactionStore, briefing
from army.bots.store import BotStore
from army.bots.supervisor import BotSupervisor
from army.bots.workloads.heartbeat import HeartbeatWorkload
from army.store import Store
from tests.army.bots.conftest import FakeOmni, StubRegistry, activate, make_bot

NOW = 1_700_000_000
HEARTBEAT = "army.bots.workloads.heartbeat:HeartbeatWorkload"


def _continuous() -> WakePolicy:
    return WakePolicy(kind=WakeKind.CONTINUOUS, precondition="always")


@pytest.fixture()
def messages(bots: BotStore) -> MessageStore:
    return MessageStore(bots)


@pytest.fixture()
def reactions(bots: BotStore) -> ReactionStore:
    return ReactionStore(bots)


def _report(bots: BotStore, messages: MessageStore) -> tuple[str, str]:
    """A bot with something in its channel worth reacting to."""
    bot = activate(bots, make_bot("scout"), now=NOW)
    message = messages.post(
        bot.id, bot.address, MessageKind.EVENT, "Tier 1 spot: 0.40% / 0.80%.", now=NOW
    )
    return bot.id, message.id


def test_a_mark_is_recorded_and_can_be_taken_back(
    bots: BotStore, messages: MessageStore, reactions: ReactionStore
) -> None:
    """The mistake people make with reactions is the mis-click."""
    bot_id, message_id = _report(bots, messages)

    assert reactions.toggle(message_id, "human:channel", "seen", now=NOW) is True
    assert reactions.for_bot(bot_id)[message_id][0].mark == "seen"

    assert reactions.toggle(message_id, "human:channel", "seen", now=NOW + 1) is False
    assert reactions.for_bot(bot_id) == {}


def test_there_is_no_mark_that_reads_as_approval() -> None:
    """A tick beside a pending question is a verdict to every human alive.

    The one thing this system must never do is let something that looks like
    consent be mistaken for it — an approval binds to an action hash, and
    nothing that does not cannot stand in for it.
    """
    assert "approve" not in MARKS
    assert "ok" not in MARKS
    assert set(MARKS) == {"seen", "useful", "unclear", "concern"}


def test_an_invented_mark_is_refused(
    bots: BotStore, messages: MessageStore, reactions: ReactionStore
) -> None:
    """A vocabulary that accepts anything means nothing."""
    _, message_id = _report(bots, messages)

    with pytest.raises(ReactionRefused, match="is not one of"):
        reactions.toggle(message_id, "human:channel", "approved", now=NOW)


def test_the_bot_is_told_once(
    bots: BotStore, messages: MessageStore, reactions: ReactionStore
) -> None:
    """A mark redelivered every brief reads as the operator repeating themselves."""
    bot_id, message_id = _report(bots, messages)
    reactions.toggle(message_id, "human:channel", "useful", now=NOW)

    assert len(reactions.take_unseen(bot_id, now=NOW + 1)) == 1
    assert reactions.take_unseen(bot_id, now=NOW + 2) == []


def test_a_mark_taken_back_before_the_bot_saw_it_is_never_mentioned(
    bots: BotStore, messages: MessageStore, reactions: ReactionStore
) -> None:
    """Retracting must actually retract, not merely stop showing."""
    bot_id, message_id = _report(bots, messages)
    reactions.toggle(message_id, "human:channel", "concern", now=NOW)
    reactions.toggle(message_id, "human:channel", "concern", now=NOW + 1)

    assert reactions.take_unseen(bot_id, now=NOW + 2) == []


def test_the_briefing_says_this_is_not_permission(
    bots: BotStore, messages: MessageStore, reactions: ReactionStore
) -> None:
    """A bot left to draw its own conclusion draws the wrong one.

    And it will draw it on the iteration where the difference matters, because
    that is the iteration where somebody was paying enough attention to react.
    """
    bot_id, message_id = _report(bots, messages)
    reactions.toggle(message_id, "human:channel", "useful", now=NOW)
    unseen = reactions.take_unseen(bot_id, now=NOW + 1)

    text = briefing(unseen, {message_id: "Tier 1 spot: 0.40% / 0.80%."})

    assert "not** permission" in text
    assert "approves" in text
    assert "0.40%" in text, "quote what was marked, or the bot cannot tell which thing"


def test_no_marks_means_no_section(bots: BotStore) -> None:
    """An empty heading in every brief is noise that teaches people to skim."""
    assert briefing([], {}) == ""


def test_concern_is_worth_acting_on_and_still_not_a_verdict(
    bots: BotStore, messages: MessageStore, reactions: ReactionStore
) -> None:
    """The two directions must not collapse into each other.

    "Somebody is uneasy" should change what a bot does next; it still is not a
    decision on anything, and a bot that treats it as a denial has invented an
    authority nobody exercised.
    """
    bot_id, message_id = _report(bots, messages)
    reactions.toggle(message_id, "human:channel", "concern", now=NOW)

    text = briefing(reactions.take_unseen(bot_id, now=NOW + 1), {message_id: "a claim"})

    assert "worth" in text
    assert "none of them as a yes" in text


def test_two_people_can_mark_the_same_message(
    bots: BotStore, messages: MessageStore, reactions: ReactionStore
) -> None:
    """Two people watching one bot is a normal Tuesday."""
    bot_id, message_id = _report(bots, messages)
    reactions.toggle(message_id, "human:channel", "seen", now=NOW)
    reactions.toggle(message_id, "human:second", "seen", now=NOW)

    assert len(reactions.for_bot(bot_id)[message_id]) == 2


def test_marking_something_that_does_not_exist_is_refused(
    messages: MessageStore,
    reactions: ReactionStore,
) -> None:
    """A mark on nothing is a row nobody can render."""
    with pytest.raises(ReactionRefused, match="no such message"):
        reactions.toggle("0" * 32, "human:channel", "seen", now=NOW)


def test_a_mark_reaches_the_bot_in_its_next_brief(store: Store, bots: BotStore) -> None:
    """A reaction only the UI can see is a nicer way of doing nothing.

    This is the difference between an acknowledgement and a decoration: the
    bot is told, and told in the same breath that it authorises nothing.
    """
    messages = MessageStore(bots)
    marks = ReactionStore(bots)
    fleet = BotSupervisor(
        store,
        FakeOmni(),
        bots,
        StubRegistry({HEARTBEAT: HeartbeatWorkload(outcome="work_done")}),
        messages=messages,
        approvals=ApprovalStore(bots, messages=messages),
        reactions=marks,
    )
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT, wake=_continuous()), now=NOW)
    posted = messages.post(
        bot.id, bot.address, MessageKind.EVENT, "Tier 1 spot: 0.40% / 0.80%.", now=NOW
    )
    marks.toggle(posted.id, "human:channel", "concern", now=NOW)

    fleet.fleet_tick(now=NOW + 1)

    run = next(entry for entry in store.list_runs() if entry.bot_id == bot.id)
    said = "\n".join(run.payload.get("said") or [])
    assert "concern" in said
    assert "not** permission" in said


def test_a_fleet_without_reactions_still_runs(store: Store, bots: BotStore) -> None:
    """Optional, like the channel and the ledger — a scheduler test needs neither."""
    messages = MessageStore(bots)
    fleet = BotSupervisor(
        store,
        FakeOmni(),
        bots,
        StubRegistry({HEARTBEAT: HeartbeatWorkload(outcome="work_done")}),
        messages=messages,
    )
    activate(bots, make_bot("scout", workload=HEARTBEAT, wake=_continuous()), now=NOW)

    assert fleet.fleet_tick(now=NOW + 1).started == 1


def test_a_mark_the_bot_already_saw_is_taken_back_out_loud(
    bots: BotStore, messages: MessageStore, reactions: ReactionStore
) -> None:
    """Deleting it would leave the bot believing something nobody can see.

    A mark briefed into an iteration is already shaping what the bot does next.
    Removing the row makes the UI honest and the bot wrong — the worst split
    available, because nothing on screen explains the behaviour.
    """
    bot_id, message_id = _report(bots, messages)
    reactions.toggle(message_id, "human:channel", "concern", now=NOW)
    reactions.take_unseen(bot_id, now=NOW + 1)

    assert reactions.toggle(message_id, "human:channel", "concern", now=NOW + 2) is False
    assert reactions.for_bot(bot_id) == {}, "the UI shows it gone"

    withdrawn = reactions.take_unseen(bot_id, now=NOW + 3)
    assert len(withdrawn) == 1, "the bot is told it was taken back"
    text = briefing(withdrawn, {message_id: "a claim"})
    assert "withdrawn" in text
    assert "taken this back" in text


def test_marking_again_after_a_retraction_is_briefed_again(
    bots: BotStore, messages: MessageStore, reactions: ReactionStore
) -> None:
    """People change their minds twice as often as once."""
    bot_id, message_id = _report(bots, messages)
    reactions.toggle(message_id, "human:channel", "useful", now=NOW)
    reactions.take_unseen(bot_id, now=NOW + 1)
    reactions.toggle(message_id, "human:channel", "useful", now=NOW + 2)
    reactions.take_unseen(bot_id, now=NOW + 3)

    assert reactions.toggle(message_id, "human:channel", "useful", now=NOW + 4) is True
    again = reactions.take_unseen(bot_id, now=NOW + 5)
    assert len(again) == 1
    assert again[0].retracted_at is None
    assert reactions.for_bot(bot_id)[message_id][0].mark == "useful"
