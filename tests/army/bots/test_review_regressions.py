"""The failures four independent reviews predicted, each pinned so it stays fixed.

Every test here exists because a reviewer read the code and said "this will
break like *this*". Most of them were right. The names describe the failure
rather than the fix, because the fix is the thing most likely to be rewritten.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from itertools import pairwise

import pytest

from army.bots.approvals import ApprovalStore
from army.bots.messages import MessageStore
from army.bots.model import BotStatus, RunOutcome, WakeKind, WakePolicy, WakeReason
from army.bots.schedule import (
    MAX_ERROR_STREAK,
    RETRY_MAX_S,
    Wake,
    first_wake,
    next_occurrence,
    next_wake,
)
from army.bots.store import BotStore
from army.bots.supervisor import BotSupervisor, wake_now
from army.bots.workloads.heartbeat import HeartbeatWorkload
from army.lanes import Lanes
from army.state import Run, RunState
from army.store import ConcurrentTransition, Store
from tests.army.bots.conftest import FakeOmni, StubRegistry, activate, make_bot

NOW = 1_700_000_000
HEARTBEAT = "army.bots.workloads.heartbeat:HeartbeatWorkload"


def _continuous(**kwargs: object) -> WakePolicy:
    kwargs.setdefault("precondition", "always")
    return WakePolicy(kind=WakeKind.CONTINUOUS, **kwargs)  # type: ignore[arg-type]


def _fleet(store: Store, bots: BotStore, workload: object | None = None) -> BotSupervisor:
    return BotSupervisor(
        store,
        FakeOmni(),
        bots,
        StubRegistry({HEARTBEAT: workload or HeartbeatWorkload(outcome="work_done")}),
        lanes=Lanes.from_config({"claude-native": 2}),
        messages=MessageStore(bots),
        approvals=ApprovalStore(bots),
    )


# ── concurrency ───────────────────────────────────────────────────


def test_two_processes_writing_at_once_wait_rather_than_failing(store: Store) -> None:
    """The deferred/immediate mix deadlocked; one policy plus a timeout does not.

    A second connection stands in for the CLI. It must be able to write while
    the supervisor holds a transaction — after waiting, not by failing with
    ``database is locked``.
    """
    other = Store(store.path)
    errors: list[BaseException] = []

    def write_from_the_cli() -> None:
        try:
            other.create_run(Run.new("cli", {"from": "cli"}, now=NOW))
        except BaseException as exc:
            errors.append(exc)

    with store.atomic() as conn:
        conn.execute(
            "INSERT INTO runs (id, workflow, state, version, attempt, created_at,"
            " updated_at, payload, artifacts, outstanding) VALUES"
            " ('held','sup','ready',0,0,?,?,'{}','{}','[]')",
            (NOW, NOW),
        )
        worker = threading.Thread(target=write_from_the_cli)
        worker.start()
        # Long enough that a no-timeout connection would already have failed.
        time.sleep(0.2)
        assert worker.is_alive(), "the second writer did not wait for the lock"
    worker.join(timeout=10)

    assert errors == [], f"a second writer failed instead of waiting: {errors}"
    assert {run.id for run in store.list_runs()} == {
        "held",
        *[r.id for r in store.list_runs() if r.workflow == "cli"],
    }
    assert any(run.workflow == "cli" for run in store.list_runs())


def test_a_nested_transaction_is_a_bug_the_code_does_not_commit(
    store: Store, bots: BotStore
) -> None:
    """Opening a second transaction inside one deadlocks against its own lock.

    Recorded as a test so the rule is executable: inside ``atomic()``, every
    store call takes the ``conn``. This asserts the hazard is real, which is
    why every method has that parameter.
    """
    with store.atomic() as conn:
        conn.execute("SELECT 1")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            # A second BEGIN IMMEDIATE on a second connection, from inside the
            # first. This is exactly what a forgotten `conn=` does.
            second = sqlite3.connect(store.path, isolation_level=None)
            try:
                second.execute("PRAGMA busy_timeout = 50")
                second.execute("BEGIN IMMEDIATE")
            finally:
                second.close()


def test_a_settled_run_cannot_write_a_wake_onto_a_bot_a_human_paused(
    store: Store, bots: BotStore
) -> None:
    """The version check alone let a succession un-pause a bot by accident.

    A run in flight when someone pauses the bot settles afterwards. Without a
    status guard it writes a due time back, so the roster shows a paused bot
    about to run and resuming it uses a wake nobody chose.
    """
    bot = activate(bots, make_bot(workload=HEARTBEAT, wake=_continuous()), now=NOW)
    stale = bots.get(bot.id)
    assert stale is not None

    bots.set_status(bot, BotStatus.PAUSED, now=NOW + 1, reason="a person stopped it")

    with pytest.raises(ConcurrentTransition, match="no longer active"):
        bots.record_wake(
            stale, Wake(NOW + 500, WakeReason.SCHEDULE, 0, 0), RunOutcome.WORK_DONE, now=NOW + 2
        )

    after = bots.get(bot.id)
    assert after is not None
    assert after.status is BotStatus.PAUSED
    assert after.next_due_at is None
    assert after.paused_reason == "a person stopped it"


def test_waking_a_bot_that_already_has_history_does_not_store_an_object(
    store: Store, bots: BotStore
) -> None:
    """``wake_now`` re-bound ``last_outcome`` as an enum rather than its value.

    The CLI's recovery for a blocked bot is the one path that passes no
    outcome, so this broke exactly when it was needed.
    """
    bot = activate(bots, make_bot(workload=HEARTBEAT, wake=_continuous()), now=NOW)
    bots.record_wake(bot, Wake(None, WakeReason.HUMAN, 0, 0), RunOutcome.BLOCKED, now=NOW)

    wake_now(bots, bot, now=NOW + 5)

    with store.atomic() as conn:
        stored = conn.execute("SELECT last_outcome FROM bots WHERE id = ?", (bot.id,)).fetchone()[
            "last_outcome"
        ]
    assert stored == RunOutcome.BLOCKED.value
    assert isinstance(stored, str)
    assert bots.get(bot.id).next_due_at == NOW + 5  # type: ignore[union-attr]


# ── the scheduler ─────────────────────────────────────────────────


def test_a_daily_bot_does_not_drift_by_one_runtime_a_day() -> None:
    """Anchoring the recurrence to "now" re-phased it on every settle.

    A bot due at 09:00 that takes twenty minutes settles at 09:20, and without
    a fixed anchor "daily" then means 09:20, then 09:41, and so on forever.
    """
    anchor = NOW
    slots = [
        next_occurrence("FREQ=DAILY", anchor + day * 86_400 + drift, anchor=anchor)
        for day, drift in enumerate((0, 1_200, 2_400, 3_600))
    ]
    assert all(slot is not None for slot in slots)
    gaps = [b - a for a, b in pairwise(slots)]  # type: ignore[operator]
    assert set(gaps) == {86_400}, f"the schedule drifted: {gaps}"


def test_a_schedule_that_has_run_out_pauses_instead_of_alarming_forever() -> None:
    """A finite rule returned None, which read as "succession lost" every tick."""
    policy = WakePolicy(kind=WakeKind.RRULE, rrule="FREQ=DAILY;COUNT=1", anchor=NOW)
    wake = next_wake(policy, RunOutcome.WORK_DONE, now=NOW + 10 * 86_400)
    assert wake.next_due_at is None
    assert wake.exhausted, "an exhausted schedule must pause the bot, not alarm about it"


def test_activating_a_bot_whose_schedule_already_ran_out_says_so() -> None:
    policy = WakePolicy(kind=WakeKind.RRULE, rrule="FREQ=DAILY;COUNT=1", anchor=NOW)
    assert first_wake(policy, now=NOW + 10 * 86_400).exhausted


def test_a_scheduled_bot_with_a_false_precondition_still_gets_capped() -> None:
    """The idle cap was continuous-only, so `FREQ=MINUTELY` polled forever."""
    policy = WakePolicy(kind=WakeKind.RRULE, rrule="FREQ=MINUTELY", anchor=NOW)
    wake = next_wake(policy, RunOutcome.NO_WORK, now=NOW, idle_streak=11)
    assert wake.exhausted
    assert wake.next_due_at is None


def test_a_pathological_but_valid_schedule_cannot_fire_faster_than_the_floor() -> None:
    """`FREQ=SECONDLY` parses fine and used to be honoured literally."""
    policy = WakePolicy(kind=WakeKind.RRULE, rrule="FREQ=SECONDLY", anchor=NOW, min_interval_s=60)
    wake = next_wake(policy, RunOutcome.WORK_DONE, now=NOW)
    assert wake.next_due_at is not None
    assert wake.next_due_at >= NOW + 60


def test_a_clock_in_milliseconds_is_refused_rather_than_killing_the_tick() -> None:
    """Only `rrulestr` used to be guarded, so the conversion raised uncaught."""
    from army.bots.model import InvalidWakePolicy

    with pytest.raises(InvalidWakePolicy):
        next_occurrence("FREQ=DAILY", NOW * 1000)


def test_the_error_ceiling_is_a_ceiling() -> None:
    """Jitter used to be added after the cap, so "15 minutes" meant thirty."""
    policy = _continuous(jitter=1.0)
    for streak in range(MAX_ERROR_STREAK - 1):
        wake = next_wake(policy, RunOutcome.RETRYABLE_ERROR, now=NOW, error_streak=streak)
        assert wake.next_due_at is not None
        assert wake.next_due_at - NOW <= RETRY_MAX_S


# ── the state machine ─────────────────────────────────────────────


def test_a_requeued_run_does_not_settle_the_iteration_it_is_still_running(
    store: Store, bots: BotStore
) -> None:
    """READY is a *live* run, so recording an outcome there was a lie.

    It also settled a second time when the run finally finished — two wakes and
    two outcomes for one iteration.
    """
    from army.bots.supervisor import _SETTLING

    assert RunState.READY not in _SETTLING


def test_a_vendor_limit_is_recorded_where_the_vendor_is_known(
    store: Store, bots: BotStore
) -> None:
    """Reading it back from artifacts meant a workload that did not record them
    produced no gate at all, silently — and after a restart every bot on that
    vendor fired at once."""
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=_continuous(), harness="claude-native"),
        now=NOW,
    )
    fleet = _fleet(store, bots)
    run = store.create_run(Run.new("heartbeat", {}, now=NOW, bot_id=bot.id))
    moved = store.transition(run, RunState.DISPATCHING, now=NOW)
    collecting = store.transition(moved, RunState.COLLECTING, now=NOW)

    fleet._requeue_rate_limited(
        collecting, ("conv_1", "claude-native", "usage limit reached"), now=NOW
    )

    gates = bots.blocked_vendors(now=NOW)
    assert "claude-native" in gates, "the vendor limit was not recorded durably"
    assert gates["claude-native"] > NOW


def test_a_cooled_vendor_still_holds_after_a_restart_clears_the_in_memory_lane(
    store: Store, bots: BotStore
) -> None:
    """The whole reason `provider_gates` exists, tested through the dispatch path.

    A run already sitting in READY because that vendor refused it is *live*, so
    the wake scan never sees it — and the first tick after a reboot used to
    send it straight back to the vendor that had just said stop.
    """
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=_continuous(), harness="claude-native"),
        now=NOW,
    )
    bots.block_vendor("claude-native", NOW + 600, "usage limit reached")
    run = store.create_run(Run.new("heartbeat", {}, now=NOW, bot_id=bot.id))

    # A brand new supervisor: in-memory lanes are empty, as after a restart.
    fresh = _fleet(store, bots)
    assert fresh._dispatch(run, now=NOW) is None, "a cooled vendor was dispatched to anyway"

    after = store.get_run(run.id)
    assert after is not None and after.state is RunState.READY

    # And it goes once the gate opens.
    assert fresh._dispatch(run, now=NOW + 601) is not None


# ── the escape hatches ────────────────────────────────────────────


def test_a_failed_retirement_is_not_reported_as_a_retirement(
    store: Store, bots: BotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claiming it retired when the write lost skipped the bot *and* left it running."""
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=_continuous(), expires_at=NOW + 10),
        now=NOW,
    )
    fleet = _fleet(store, bots)

    def lose(*args: object, **kwargs: object) -> None:
        raise ConcurrentTransition("someone else moved it")

    monkeypatch.setattr(bots, "set_status", lose)
    assert fleet._retire_if_expired(bot, now=NOW + 11) is False


def test_a_bot_expiring_while_blocked_on_a_person_is_still_retired(
    store: Store, bots: BotStore
) -> None:
    """Expiry used to be checked only in the due scan, which a blocked bot never
    reaches — so the bot most likely to be forgotten was the one that could not
    be retired."""
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=_continuous(), expires_at=NOW + 10),
        now=NOW,
    )
    bots.record_wake(bot, Wake(None, WakeReason.HUMAN, 0, 0), RunOutcome.BLOCKED, now=NOW)
    assert bots.due(now=NOW + 11) == [], "the blocked bot must not be in the due scan"

    _fleet(store, bots).fleet_tick(now=NOW + 11)
    assert bots.get(bot.id).status is BotStatus.RETIRED  # type: ignore[union-attr]


def test_the_stalled_alarm_fires_once_not_once_per_tick(
    store: Store, bots: BotStore, caplog: pytest.LogCaptureFixture
) -> None:
    """An alarm that fires every ten seconds is one nobody reads on the night it
    finally means something."""
    bot = activate(bots, make_bot(workload=HEARTBEAT, wake=_continuous()), now=NOW)
    bots.record_wake(bot, Wake(None, WakeReason.SCHEDULE, 0, 0), None, now=NOW)
    fleet = _fleet(store, bots)

    with caplog.at_level("ERROR"):
        for tick in range(5):
            fleet.fleet_tick(now=NOW + tick)
    alarms = [r for r in caplog.records if "succession" in r.getMessage()]
    assert len(alarms) == 1, f"the same alarm fired {len(alarms)} times"


def test_a_bot_parked_on_a_question_is_not_reported_as_lost(
    store: Store, bots: BotStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Waiting on a person is the normal case, not a fault."""
    activate(bots, make_bot(workload=HEARTBEAT, wake=_continuous()), now=NOW)
    fleet = _fleet(store, bots)
    for tick in range(4):
        fleet.tick(now=NOW + tick)

    with caplog.at_level("ERROR"):
        fleet.fleet_tick(now=NOW + 10)
    assert not [r for r in caplog.records if "succession" in r.getMessage()]


def test_a_system_pause_records_why_it_happened(store: Store, bots: BotStore) -> None:
    """Otherwise it is indistinguishable from a person stopping it on purpose,
    and nobody restarts it once the cause is fixed."""
    bot = activate(
        bots,
        make_bot(workload=HEARTBEAT, wake=_continuous(precondition="not-registered")),
        now=NOW,
    )
    _fleet(store, bots).tick(now=NOW)

    paused = bots.get(bot.id)
    assert paused is not None
    assert paused.status is BotStatus.PAUSED
    assert paused.paused_reason is not None
    assert "precondition" in paused.paused_reason


def test_a_refused_run_hands_the_work_item_back(store: Store, bots: BotStore) -> None:
    """`acquire` has a side effect, so an item taken for a refused run is lost."""

    class ReleasingWorkload(HeartbeatWorkload):
        name = "releasing"

        def __init__(self) -> None:
            super().__init__()
            self.released: list[dict] = []

        def release(self, item: dict) -> None:
            self.released.append(item)

    workload = ReleasingWorkload()
    bot = activate(bots, make_bot(workload=HEARTBEAT, wake=_continuous()), now=NOW)
    fleet = _fleet(store, bots, workload)

    # A live run appears between the check and the insert.
    original = fleet._has_live_run
    monkey = {"first": True}

    def pretend_free(candidate: object) -> bool:
        if monkey["first"]:
            monkey["first"] = False
            store.create_run(Run.new("releasing", {}, now=NOW, bot_id=bot.id))
            return False
        return original(candidate)  # type: ignore[arg-type]

    fleet._has_live_run = pretend_free  # type: ignore[method-assign]
    assert fleet._start(bot, now=NOW) is None
    assert workload.released, "the work item was not handed back"
