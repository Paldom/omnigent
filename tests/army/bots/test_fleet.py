"""The loop a person actually watches: a bot wakes, works, asks, and backs off.

These are the behaviours the day-one path exercises, each pinned so a later
refactor cannot quietly change what the operator sees. Three of them exist
because driving the real CLI found the bug first.
"""

from __future__ import annotations

import pytest

from army.bots.model import BotStatus, DerivedStatus, RunOutcome, WakeKind, WakePolicy
from army.bots.precondition import PreconditionRegistry
from army.bots.roster import roster
from army.bots.store import BotStore
from army.bots.supervisor import BotSupervisor, classify
from army.bots.workloads.heartbeat import HeartbeatWorkload
from army.state import CommandKind, Run, RunState
from army.store import Store
from tests.army.bots.conftest import FakeOmni, StubRegistry, activate, make_bot

NOW = 1_700_000_000

HEARTBEAT = "army.bots.workloads.heartbeat:HeartbeatWorkload"


def _continuous(**kwargs: object) -> WakePolicy:
    kwargs.setdefault("precondition", "always")
    return WakePolicy(kind=WakeKind.CONTINUOUS, **kwargs)  # type: ignore[arg-type]


def _fleet(
    store: Store,
    bots: BotStore,
    workload: HeartbeatWorkload,
    *,
    preconditions: PreconditionRegistry | None = None,
) -> BotSupervisor:
    return BotSupervisor(
        store,
        FakeOmni(),
        bots,
        StubRegistry({HEARTBEAT: workload}),
        preconditions=preconditions,
    )


def _drive(fleet: BotSupervisor, ticks: int, *, start: int = NOW, step: int = 1) -> None:
    for index in range(ticks):
        fleet.tick(now=start + index * step)


def test_a_bot_with_no_sessions_still_asks(store: Store, bots: BotStore) -> None:
    """An iteration that spawned no session must still reach a person.

    The base supervisor posts the question into an Omnigent session and fails
    the run when there is none. For a bot that is wrong twice over: the
    question is the point of the iteration, and the row is the record — the
    session is only where it is also shown.
    """
    bot = activate(bots, make_bot(workload=HEARTBEAT, wake=_continuous()), now=NOW)
    fleet = _fleet(store, bots, HeartbeatWorkload(outcome="work_done"))
    _drive(fleet, 4)

    run = store.list_runs()[0]
    assert run.state is RunState.WAITING_HUMAN, "an iteration with no session failed instead"
    assert run.approval_id == f"barrier_{run.id}"
    assert run.artifacts["question"].startswith("Heartbeat 1 finished")
    assert run.artifacts["options"] == ["continue", "stop"]

    parked = bots.get(bot.id)
    assert parked is not None and parked.next_due_at is None


def test_parking_on_a_person_is_not_an_idle_iteration(store: Store, bots: BotStore) -> None:
    """A bot must not be backed off for having asked a question.

    ``collect`` declares its outcome before the iteration settles, so a run
    that then parks still carries that declaration. Letting it win counted
    every approval as "nothing to do" and doubled the idle streak.
    """
    bot = activate(bots, make_bot(workload=HEARTBEAT, wake=_continuous()), now=NOW)
    fleet = _fleet(store, bots, HeartbeatWorkload(outcome="no_work"))
    _drive(fleet, 4)

    parked = bots.get(bot.id)
    assert parked is not None
    assert parked.idle_streak == 0, "asking a question counted as an empty iteration"
    assert parked.last_outcome is RunOutcome.BLOCKED

    run = store.list_runs()[0]
    fleet.answer(run.id, CommandKind.APPROVE, {"choice": "continue"}, now=NOW + 10)
    fleet.tick(now=NOW + 11)

    after = bots.get(bot.id)
    assert after is not None
    assert after.idle_streak == 1, "the settled iteration should be the only idle one"
    assert after.last_outcome is RunOutcome.NO_WORK


@pytest.mark.parametrize(
    ("target", "declared", "expected"),
    [
        (RunState.WAITING_HUMAN, "work_done", RunOutcome.BLOCKED),
        (RunState.PAUSED, "work_done", RunOutcome.BLOCKED),
        (RunState.FAILED, "work_done", RunOutcome.RETRYABLE_ERROR),
        (RunState.CONTINUE, "no_work", RunOutcome.NO_WORK),
        (RunState.CONTINUE, "work_done", RunOutcome.WORK_DONE),
        (RunState.COMPLETED, "no_work", RunOutcome.NO_WORK),
        (RunState.CONTINUE, None, RunOutcome.WORK_DONE),
        # A workload may only choose between these two, and an unknown value
        # fails toward the cautious one rather than the expensive one.
        (RunState.CONTINUE, "nonsense", RunOutcome.NO_WORK),
        (RunState.CONTINUE, "blocked", RunOutcome.NO_WORK),
        (RunState.COMPLETED, "rate_limited", RunOutcome.NO_WORK),
    ],
)
def test_only_a_clean_finish_lets_the_workload_choose(
    target: RunState, declared: str | None, expected: RunOutcome
) -> None:
    """The move decides, except where the move is genuinely ambiguous.

    And even then the workload may only pick between "I did something" and
    "there was nothing to do". Letting it declare BLOCKED on a terminal move
    would write ``next_due_at = NULL`` for a run that has ended, and nothing
    would ever wake the bot again.
    """
    artifacts = {"outcome": declared} if declared is not None else {}
    run = Run.new("w", {}, now=NOW, bot_id="b")
    run.artifacts = artifacts
    assert classify(run, target) is expected


def test_an_empty_precondition_costs_no_run_at_all(store: Store, bots: BotStore) -> None:
    """The economic claim: an idle fleet spends queries, not vendor turns."""
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=_continuous(precondition="never")),
        now=NOW,
    )
    workload = HeartbeatWorkload()
    fleet = _fleet(store, bots, workload)

    # Follow the schedule rather than the wall clock: after each empty wake the
    # bot is not due again for a while, and ticking inside that window is
    # supposed to do nothing at all.
    delays = []
    clock = NOW
    for _ in range(3):
        fleet.tick(now=clock)
        current = bots.get(bot.id)
        assert current is not None and current.next_due_at is not None
        delays.append(current.next_due_at - clock)
        # A tick before the next wake must be a complete no-op.
        fleet.tick(now=clock + 1)
        assert bots.get(bot.id).idle_streak == current.idle_streak  # type: ignore[union-attr]
        clock = current.next_due_at

    assert store.list_runs() == [], "a body was spawned for a bot with nothing to do"
    assert workload.beats == 0, "work was acquired despite the precondition saying no"
    assert delays == sorted(delays), f"an idle bot did not get further away: {delays}"

    backed_off = bots.get(bot.id)
    assert backed_off is not None
    assert backed_off.idle_streak == 3


def test_a_bot_naming_an_unregistered_precondition_is_paused_not_silent(
    store: Store, bots: BotStore
) -> None:
    """An operator typo must not look like a permanently quiet bot."""
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=_continuous(precondition="qeue_is_empty")),
        now=NOW,
    )
    fleet = _fleet(store, bots, HeartbeatWorkload())
    fleet.tick(now=NOW)

    after = bots.get(bot.id)
    assert after is not None and after.status is BotStatus.PAUSED


def test_one_tick_opens_one_run_however_many_bots_are_due(store: Store, bots: BotStore) -> None:
    """A fleet that all comes due at once must not open ten sessions at once."""
    for index in range(5):
        activate(bots, make_bot(f"bot-{index}", workload=HEARTBEAT, wake=_continuous()), now=NOW)
    fleet = _fleet(store, bots, HeartbeatWorkload())
    fleet.tick(now=NOW)
    assert len(store.list_runs()) == 1


def test_a_cooled_vendor_holds_its_bots_without_backing_them_off(
    store: Store, bots: BotStore
) -> None:
    """The gate is shared, so a bot must not also serve a private sentence."""
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=_continuous(), harness="claude-native"),
        now=NOW,
    )
    bots.block_vendor("claude-native", NOW + 300, "usage limit reached")
    fleet = _fleet(store, bots, HeartbeatWorkload())
    fleet.tick(now=NOW)

    assert store.list_runs() == []
    held = bots.get(bot.id)
    assert held is not None
    assert held.idle_streak == 0, "a vendor limit was charged to the bot as an idle iteration"
    assert held.next_due_at == NOW, "the bot lost its place in the queue"

    entry = next(row for row in roster(bots, now=NOW) if row.bot.id == bot.id)
    assert entry.status is DerivedStatus.WAITING_RESOURCE
    assert entry.blocked_until == NOW + 300


def test_an_expired_bot_retires_itself(store: Store, bots: BotStore) -> None:
    """A spawned bot with a TTL must not outlive it just because nobody looked."""
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=_continuous(), expires_at=NOW + 10),
        now=NOW,
    )
    fleet = _fleet(store, bots, HeartbeatWorkload())
    fleet.tick(now=NOW + 11)

    after = bots.get(bot.id)
    assert after is not None and after.status is BotStatus.RETIRED
    assert store.list_runs() == []


def test_the_roster_puts_what_needs_a_person_first(store: Store, bots: BotStore) -> None:
    """The question it exists to answer at a glance: which one is stuck?"""
    activate(bots, make_bot("quiet", workload=HEARTBEAT, wake=_continuous()), now=NOW)
    asking = activate(bots, make_bot("asking", workload=HEARTBEAT, wake=_continuous()), now=NOW)
    # Left in DRAFT, so the roster has something inactive to sort last.
    bots.create(make_bot("drafted", workload=HEARTBEAT, wake=_continuous()))

    run = store.create_run(Run.new("heartbeat", {}, now=NOW, bot_id=asking.id))
    store.transition(run, RunState.DISPATCHING, now=NOW)
    collecting = store.get_run(run.id)
    assert collecting is not None
    store.transition(collecting, RunState.COLLECTING, now=NOW)
    evaluating = store.get_run(run.id)
    assert evaluating is not None
    store.transition(evaluating, RunState.EVALUATING, now=NOW)
    parked = store.get_run(run.id)
    assert parked is not None
    store.transition(parked, RunState.WAITING_HUMAN, now=NOW)

    entries = roster(bots, now=NOW)
    assert next(entry.bot.slug for entry in entries) == "asking"
    assert entries[0].status is DerivedStatus.WAITING_HUMAN
    assert entries[0].needs_a_human
    assert entries[-1].status is DerivedStatus.INACTIVE


def test_the_heartbeat_loops_without_a_vendor(store: Store, bots: BotStore) -> None:
    """The day-one path, end to end, with no Omnigent session anywhere.

    If this does not loop, nothing else in Bot mode is worth debugging.
    """
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=_continuous(min_interval_s=60)),
        now=NOW,
    )
    workload = HeartbeatWorkload(outcome="work_done")
    fleet = _fleet(store, bots, workload)

    settled = 0
    clock = NOW
    for _ in range(3):
        _drive(fleet, 4, start=clock)
        run = next(r for r in store.list_runs() if r.state is RunState.WAITING_HUMAN)
        fleet.answer(run.id, CommandKind.APPROVE, {"choice": "continue"}, now=clock + 5)
        fleet.tick(now=clock + 6)
        settled += 1
        # Jump to whatever the scheduler decided, which is the point.
        current = bots.get(bot.id)
        assert current is not None and current.next_due_at is not None
        clock = current.next_due_at

    assert settled == 3
    assert workload.beats == 3
    finished = [r for r in store.list_runs() if r.state is RunState.CONTINUE]
    assert len(finished) == 3
    assert all(r.outcome == RunOutcome.WORK_DONE.value for r in finished)
    assert all(r.revision_id == bot.current_revision_id for r in finished)
