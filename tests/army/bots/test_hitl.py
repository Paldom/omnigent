"""Asking a person, and the paths that must not be able to answer instead.

The integration the two halves have to get right: the supervisor parks a run,
the ledger holds a question bound to that exact iteration, and the only way
forward is a verdict that still matches. Everything else — a reply typed into a
session, a stale answer, silence — leaves the bot where it was.
"""

from __future__ import annotations

import pytest

from army.bots.approvals import ApprovalState, ApprovalStore
from army.bots.messages import MessageKind, MessageStore
from army.bots.model import BotStatus, RunOutcome, WakeKind, WakePolicy
from army.bots.store import BotStore
from army.bots.supervisor import BotSupervisor
from army.bots.workloads.heartbeat import HeartbeatWorkload
from army.state import CommandKind, RunState
from army.store import Store
from tests.army.bots.conftest import (
    CountingWorkload,
    FakeOmni,
    StubRegistry,
    activate,
    make_bot,
)

NOW = 1_700_000_000
HEARTBEAT = "army.bots.workloads.heartbeat:HeartbeatWorkload"
COUNTING = "tests:counting"


def _continuous(**kwargs: object) -> WakePolicy:
    kwargs.setdefault("precondition", "always")
    return WakePolicy(kind=WakeKind.CONTINUOUS, **kwargs)  # type: ignore[arg-type]


def _fleet(
    store: Store, bots: BotStore, omni: FakeOmni | None = None
) -> tuple[BotSupervisor, MessageStore, ApprovalStore]:
    messages = MessageStore(bots)
    approvals = ApprovalStore(bots)
    fleet = BotSupervisor(
        store,
        omni or FakeOmni(),
        bots,
        StubRegistry({HEARTBEAT: HeartbeatWorkload(outcome="work_done")}),
        messages=messages,
        approvals=approvals,
    )
    return fleet, messages, approvals


def _park(fleet: BotSupervisor, *, start: int = NOW) -> None:
    for index in range(4):
        fleet.tick(now=start + index)


def test_an_ask_becomes_a_row_a_message_and_a_parked_run(store: Store, bots: BotStore) -> None:
    """All three, or the question is not really addressable."""
    bot = activate(bots, make_bot(workload=HEARTBEAT, wake=_continuous()), now=NOW)
    fleet, messages, approvals = _fleet(store, bots)
    _park(fleet)

    run = store.list_runs()[0]
    assert run.state is RunState.WAITING_HUMAN

    request = approvals.open_for_run(run.id)
    assert request is not None
    assert request.bot_id == bot.id
    assert request.options == ["continue", "stop"]
    assert request.thread_id == run.id

    posted = messages.thread(bot.id, run.id)
    assert [m.kind for m in posted] == [MessageKind.ASK]
    assert posted[0].payload["approval_id"] == request.id


def test_the_verdict_binds_the_version_the_run_is_parked_at(store: Store, bots: BotStore) -> None:
    """Binding the pre-move version made every verdict look stale at birth.

    The move into ``WAITING_HUMAN`` is a compare-and-swap, so it lands at
    exactly one more than the version the ask read — and that is the version a
    person is answering against.
    """
    activate(bots, make_bot(workload=HEARTBEAT, wake=_continuous()), now=NOW)
    fleet, _, approvals = _fleet(store, bots)
    _park(fleet)

    run = store.list_runs()[0]
    request = approvals.open_for_run(run.id)
    assert request is not None
    assert request.run_version == run.version, "the ask bound a version the run never had"

    command = approvals.decide(
        request,
        approved=True,
        decided_by="human:dpal",
        now=NOW + 10,
        choice="continue",
        run_version=run.version,
    )
    assert command.kind is CommandKind.APPROVE

    fleet.tick(now=NOW + 11)
    settled = store.get_run(run.id)
    assert settled is not None and settled.state is RunState.CONTINUE
    assert settled.outcome == RunOutcome.WORK_DONE.value


def test_a_reply_typed_into_a_session_does_not_authorise_anything(
    store: Store, bots: BotStore
) -> None:
    """A binding one answer path ignores is not a binding.

    The base supervisor turns a reply naming an offered option into an
    ``APPROVE``. For a bot that is a second authorisation route consulting none
    of the bindings, so it is refused — and the words are kept where a person
    can see them.
    """
    bot = activate(bots, make_bot(workload=COUNTING, wake=_continuous()), now=NOW)
    omni = FakeOmni()
    # A workload that opens a real session, because the path being refused is
    # the one that reads a session transcript.
    messages_store = MessageStore(bots)
    approvals = ApprovalStore(bots)
    fleet = BotSupervisor(
        store,
        omni,
        bots,
        StubRegistry({COUNTING: CountingWorkload(name=COUNTING, items=[{"task": "one"}])}),
        messages=messages_store,
        approvals=approvals,
    )
    messages = messages_store
    _park(fleet)
    run = store.list_runs()[0]

    omni.replies = ["continue"]
    fleet.tick(now=NOW + 10)

    still_parked = store.get_run(run.id)
    assert still_parked is not None
    assert still_parked.state is RunState.WAITING_HUMAN, "typed prose authorised an iteration"
    assert approvals.open_for_run(run.id) is not None

    said = [m for m in messages.thread(bot.id, run.id) if m.kind is MessageKind.HUMAN_MSG]
    assert [m.body for m in said] == ["continue"]
    assert said[0].payload["authorises"] is False


def test_a_question_nobody_answers_pauses_the_bot(store: Store, bots: BotStore) -> None:
    """Never auto-approve. The bot stops and says so, which a person can see."""
    bot = activate(bots, make_bot(workload=HEARTBEAT, wake=_continuous()), now=NOW)
    fleet, messages, approvals = _fleet(store, bots)
    _park(fleet)
    run = store.list_runs()[0]
    request = approvals.open_for_run(run.id)
    assert request is not None and request.expires_at is not None

    fleet.fleet_tick(now=request.expires_at + 1)

    expired = approvals.get(request.id)
    assert expired is not None and expired.state is ApprovalState.EXPIRED
    paused = bots.get(bot.id)
    assert paused is not None and paused.status is BotStatus.PAUSED
    assert any(
        m.kind is MessageKind.EVENT and "expired" in m.body.lower()
        for m in messages.channel(bot.id)
    )


def test_mail_wakes_an_event_driven_bot_in_the_same_tick(store: Store, bots: BotStore) -> None:
    """An ``on_message`` bot has no schedule; an insert is the only thing that
    should wake it, and it should not have to wait a tick to notice."""
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=WakePolicy(kind=WakeKind.ON_MESSAGE)),
        now=NOW,
    )
    fleet, messages, _ = _fleet(store, bots)
    assert bots.get(bot.id).next_due_at is None  # type: ignore[union-attr]

    fleet.fleet_tick(now=NOW)
    assert store.list_runs() == [], "an event bot ran with no event"

    messages.post(
        bot.id,
        "human:dpal",
        MessageKind.HUMAN_MSG,
        "look at this",
        now=NOW + 5,
        deliver_to=[bot.address],
    )
    fleet.fleet_tick(now=NOW + 6)
    assert len(store.list_runs()) == 1, "mail did not wake the bot in the same tick"


def test_mail_does_not_pull_a_scheduled_bot_forward(store: Store, bots: BotStore) -> None:
    """Two bots that talk to each other would otherwise spin with no human.

    A scheduled bot keeps its schedule; only a bot with nothing scheduling it
    is woken by an insert.
    """
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=_continuous(min_interval_s=3600)),
        now=NOW,
    )
    bots.record_wake(
        bot,
        __import__("army.bots.schedule", fromlist=["Wake"]).Wake(
            NOW + 3600, bot.wake_reason, 0, 0
        ),
        None,
        now=NOW,
    )
    fleet, messages, _ = _fleet(store, bots)
    messages.post(
        bot.id, "bot:other", MessageKind.BOT_TO_BOT, "ping", now=NOW, deliver_to=[bot.address]
    )
    fleet.fleet_tick(now=NOW + 1)

    unchanged = bots.get(bot.id)
    assert unchanged is not None and unchanged.next_due_at == NOW + 3600
    assert store.list_runs() == []


def test_a_withdrawn_question_does_not_linger_when_the_run_fails_to_park(
    store: Store, bots: BotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unanswerable question in the pending list is worse than none."""
    activate(bots, make_bot(workload=HEARTBEAT, wake=_continuous()), now=NOW)
    fleet, _, approvals = _fleet(store, bots)
    for index in range(3):
        fleet.tick(now=NOW + index)

    original = fleet._transition

    def refuse(run, target, **kwargs):  # type: ignore[no-untyped-def]
        if target is RunState.WAITING_HUMAN:
            raise RuntimeError("the parking write lost its race")
        return original(run, target, **kwargs)

    monkeypatch.setattr(fleet, "_transition", refuse)
    fleet.tick(now=NOW + 4)

    assert approvals.pending() == [], "a question was left pending for a run that never parked"
