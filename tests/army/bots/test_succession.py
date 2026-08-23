"""The property the whole design turns on: an iteration ends and the next one
is scheduled, together or not at all.

Written before the succession code it tests, because three independent reviews
predicted the same failure and named the same symptom: a crash between the
terminal transition and the ``next_due_at`` write leaves a bot whose work is
done and whose next wake is ``NULL`` — and ``NULL <= now`` is false in SQLite,
so the scan skips it and the bot sleeps forever.
"""

from __future__ import annotations

import pytest

from army.bots.model import BotStatus, RunOutcome, WakeKind, WakePolicy, WakeReason
from army.bots.store import BotStore
from army.bots.supervisor import BotSupervisor
from army.state import CommandKind, Run, RunState
from army.store import ConcurrentTransition, Store
from tests.army.bots.conftest import (
    CountingWorkload,
    FakeOmni,
    StubRegistry,
    activate,
    make_bot,
)

NOW = 1_700_000_000


def _fleet(store: Store, bots: BotStore, workload: CountingWorkload) -> BotSupervisor:
    """A supervisor driving one workload, resolved without importing anything."""
    return BotSupervisor(
        store,
        FakeOmni(),
        bots,
        StubRegistry({"tests:counting": workload}),
    )


def _continuous(**kwargs: object) -> WakePolicy:
    """A continuous policy with the mandatory precondition satisfied."""
    return WakePolicy(kind=WakeKind.CONTINUOUS, precondition="always", **kwargs)  # type: ignore[arg-type]


def test_a_finished_iteration_schedules_the_next_one(store: Store, bots: BotStore) -> None:
    """The happy path: work done, so the bot is due again at its floor."""
    workload = CountingWorkload(items=[{"task": "one"}, {"task": "two"}])
    bot = activate(bots, make_bot(wake=_continuous(min_interval_s=60)), now=NOW)
    fleet = _fleet(store, bots, workload)

    for step in range(4):
        fleet.tick(now=NOW + step)

    run = store.list_runs()[0]
    assert run.state is RunState.WAITING_HUMAN
    assert run.outcome == RunOutcome.BLOCKED.value

    parked = bots.get(bot.id)
    assert parked is not None
    # Blocked on a person costs nothing: no wake at all until the verdict.
    assert parked.next_due_at is None
    assert parked.wake_reason is WakeReason.HUMAN

    fleet.answer(run.id, CommandKind.APPROVE, {"choice": "yes"}, now=NOW + 10)
    fleet.tick(now=NOW + 11)

    resumed = bots.get(bot.id)
    assert resumed is not None
    assert resumed.last_outcome is RunOutcome.WORK_DONE
    assert resumed.next_due_at == NOW + 11 + 60
    assert resumed.idle_streak == 0


def test_a_crash_between_the_two_writes_leaves_neither(
    store: Store, bots: BotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill test. If the wake write fails, the run must not have moved.

    This is the one that catches the mistake every reviewer predicted: calling
    an existing ``Store`` method that opens its own connection, so the
    transition commits on its own and the rollback cannot reach it.
    """
    workload = CountingWorkload(items=[{"task": "one"}])
    activate(bots, make_bot(wake=_continuous()), now=NOW)
    fleet = _fleet(store, bots, workload)

    for step in range(4):
        fleet.tick(now=NOW + step)
    run = store.list_runs()[0]
    assert run.state is RunState.WAITING_HUMAN
    version_when_parked = run.version

    fleet.answer(run.id, CommandKind.APPROVE, {"choice": "yes"}, now=NOW + 10)

    def die(*args: object, **kwargs: object) -> None:
        raise RuntimeError("power cut, between the two writes")

    monkeypatch.setattr(bots, "record_wake", die)
    # The pass survives; the run does not move. Nothing is half-done, which is
    # the entire claim.
    fleet.tick(now=NOW + 11)

    after = store.get_run(run.id)
    assert after is not None
    assert after.state is RunState.WAITING_HUMAN, "the transition committed without its wake"
    assert after.version == version_when_parked, "a rolled-back move still bumped the version"
    # Still the outcome it parked with, not the one the rolled-back move carried.
    assert after.outcome == RunOutcome.BLOCKED.value

    command = store.next_command(run.id)
    assert command is not None, "the command was consumed by a move that did not land"

    # And once the store works again, the answer that was waiting still applies.
    monkeypatch.undo()
    fleet.tick(now=NOW + 12)
    recovered = store.get_run(run.id)
    assert recovered is not None and recovered.state is RunState.CONTINUE
    assert store.next_command(run.id) is None


def test_replaying_the_same_succession_is_a_no_op(store: Store, bots: BotStore) -> None:
    """A second attempt at a move that already landed loses the CAS, quietly."""
    workload = CountingWorkload(items=[{"task": "one"}])
    activate(bots, make_bot(wake=_continuous()), now=NOW)
    fleet = _fleet(store, bots, workload)
    for step in range(4):
        fleet.tick(now=NOW + step)

    run = store.list_runs()[0]
    fleet.answer(run.id, CommandKind.APPROVE, {"choice": "yes"}, now=NOW + 10)
    fleet.tick(now=NOW + 11)
    settled = store.get_run(run.id)
    assert settled is not None and settled.state is RunState.CONTINUE

    # Replay the move from the stale read. The version has moved on, so it
    # loses rather than producing a second iteration.
    with pytest.raises(ConcurrentTransition):
        fleet._transition(run, RunState.CONTINUE, now=NOW + 12)

    assert len([r for r in store.list_runs() if r.state is RunState.CONTINUE]) == 1


def test_a_blocked_bot_is_woken_by_an_insert_not_by_time(store: Store, bots: BotStore) -> None:
    """``next_due_at IS NULL`` is not "soon"; it is "never, until something happens"."""
    workload = CountingWorkload(items=[{"task": "one"}])
    bot = activate(bots, make_bot(wake=_continuous()), now=NOW)
    fleet = _fleet(store, bots, workload)
    for step in range(4):
        fleet.tick(now=NOW + step)

    blocked = bots.get(bot.id)
    assert blocked is not None and blocked.next_due_at is None
    # A year later it is still not due. Time cannot wake it.
    assert bots.due(now=NOW + 31_536_000) == []


def test_a_lost_bump_is_visible_rather_than_silent(store: Store, bots: BotStore) -> None:
    """The backstop for a succession that got lost anyway.

    A bot that is active, has no live run and has no next wake has fallen
    through a crack. Event-driven and manual bots legitimately have no wake, so
    they must not appear here.
    """
    stranded = activate(bots, make_bot("stranded", wake=_continuous()), now=NOW)
    bots.record_wake(
        stranded,
        __import__("army.bots.schedule", fromlist=["Wake"]).Wake(None, WakeReason.SCHEDULE, 0, 0),
        None,
        now=NOW,
    )

    listening = make_bot("listening", wake=WakePolicy(kind=WakeKind.ON_MESSAGE))
    activate(bots, listening, now=NOW)
    manual = make_bot("manual", wake=WakePolicy(kind=WakeKind.MANUAL))
    activate(bots, manual, now=NOW)

    alarmed = {bot.slug for bot in bots.stalled(now=NOW)}
    assert alarmed == {"stranded"}


def test_paused_stays_inside_the_one_live_run_index(store: Store, bots: BotStore) -> None:
    """Excluding ``paused`` would let a tick open a second run behind a paused one.

    The three excluded states are hard-coded rather than derived from
    ``army.state.TERMINAL``, which contains ``PAUSED``. Deriving them is the
    mistake; this test is what stops someone "simplifying" it back.
    """
    bot = activate(bots, make_bot(wake=_continuous()), now=NOW)
    first = store.create_run(Run.new("tests:counting", {}, now=NOW, bot_id=bot.id))
    store.transition(first, RunState.DISPATCHING, now=NOW)
    paused = store.get_run(first.id)
    assert paused is not None
    store.transition(paused, RunState.FAILED, now=NOW)

    # A failed run is outside the index, so a successor is allowed.
    store.create_run(Run.new("tests:counting", {}, now=NOW + 1, bot_id=bot.id))

    # A second live run is not.
    with pytest.raises(ConcurrentTransition):
        store.create_run(Run.new("tests:counting", {}, now=NOW + 2, bot_id=bot.id))

    assert "paused" not in _index_predicate(store)


def test_a_bot_with_no_id_is_not_covered_by_the_index(store: Store) -> None:
    """Ordinary workload runs keep working; the index only speaks about bots."""
    store.create_run(Run.new("plain", {}, now=NOW))
    store.create_run(Run.new("plain", {}, now=NOW + 1))
    assert len(store.list_runs()) == 2


def _index_predicate(store: Store) -> str:
    """The SQL of the one-live-run index, for asserting on its exclusions."""
    with store.atomic() as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'one_live_run_per_bot'"
        ).fetchone()
    return str(row["sql"])


def test_the_index_excludes_exactly_three_states(store: Store) -> None:
    """Named so a future reader sees which three, and why not four."""
    predicate = _index_predicate(store)
    for terminal in ("continue", "completed", "failed"):
        assert terminal in predicate
    assert "paused" not in predicate
    assert "waiting_human" not in predicate


def test_activation_and_first_wake_land_together(store: Store, bots: BotStore) -> None:
    """A bot must never be ACTIVE with no schedule; the stalled scan would flag it."""
    bot = make_bot(wake=_continuous())
    bots.create(bot)
    assert bot.status is BotStatus.DRAFT
    assert bots.due(now=NOW) == []

    activate_result = activate(bots, make_bot("other", wake=_continuous()), now=NOW)
    assert activate_result.status is BotStatus.ACTIVE
    assert activate_result.next_due_at == NOW
    assert bots.stalled(now=NOW) == []


def test_pausing_a_bot_clears_its_wake(store: Store, bots: BotStore) -> None:
    """A paused bot that keeps a due time reads as "about to run" in the roster."""
    bot = activate(bots, make_bot(wake=_continuous()), now=NOW)
    assert bot.next_due_at is not None
    bots.set_status(bot, BotStatus.PAUSED, now=NOW + 1)
    assert bot.next_due_at is None
    assert bots.due(now=NOW + 100) == []


def test_two_ticks_racing_one_due_bot_produce_one_run(store: Store, bots: BotStore) -> None:
    """The index is the arbiter, and the loser is told it lost."""
    bot = activate(bots, make_bot(wake=_continuous()), now=NOW)
    store.create_run(Run.new("tests:counting", {"task": "a"}, now=NOW, bot_id=bot.id))
    with pytest.raises(ConcurrentTransition):
        store.create_run(Run.new("tests:counting", {"task": "b"}, now=NOW, bot_id=bot.id))
    assert len(store.list_runs()) == 1


def test_a_taken_item_is_never_stranded_by_a_refused_run(
    store: Store, bots: BotStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A bot that already has a run must not have work acquired for it.

    ``acquire()`` has a side effect — the demo workload marks its queue line
    ``taken:`` — so taking an item for a run the index then refuses loses the
    item. The live-run check runs before the acquire for exactly this reason.
    """
    workload = CountingWorkload(items=[{"task": "one"}])
    bot = activate(bots, make_bot(wake=_continuous()), now=NOW)
    store.create_run(Run.new("tests:counting", {"task": "existing"}, now=NOW, bot_id=bot.id))

    fleet = _fleet(store, bots, workload)
    assert fleet._start(bot, now=NOW) is None
    assert workload.acquired == [], "work was taken for a bot that was already busy"
    assert workload.items == [{"task": "one"}]
