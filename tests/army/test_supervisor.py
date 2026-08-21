"""The durability properties the control plane exists to provide.

The headline test is :func:`test_run_survives_a_restart_and_advances_once`. It
is the acceptance criterion in one function: a run reaches ``WAITING_HUMAN``,
every object holding it in memory is destroyed, a verdict arrives afterwards,
and the run advances by exactly one state. If that ever fails, the loop is back
to being an in-memory ``while True`` with extra steps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from army.lanes import Lane, Lanes
from army.omni import OmniError
from army.state import Command, CommandKind, IllegalTransition, Run, RunState
from army.store import ConcurrentTransition, Store
from army.supervisor import MAX_ATTEMPTS, Supervisor


class FakeOmni:
    """Stands in for the Omnigent server.

    Records what the supervisor asked it to do, and can be told to fail in the
    two ways that matter: transiently (retry) and permanently (give up).
    """

    def __init__(self) -> None:
        self.sessions: list[str] = []
        self.asked: list[tuple[str, str, list[str]]] = []
        self.fail_ask: OmniError | None = None
        self.fail_dispatch: OmniError | None = None

    def create_session(self, agent_id: str, **kwargs: Any) -> str:
        session_id = f"conv_{len(self.sessions):032d}"
        self.sessions.append(session_id)
        return session_id

    def send(self, session_id: str, text: str) -> None:
        pass

    def ask(
        self,
        session_id: str,
        message: str,
        options: list[str],
        *,
        evidence: dict[str, Any] | None = None,
    ) -> str:
        if self.fail_ask is not None:
            raise self.fail_ask
        self.asked.append((session_id, message, options))
        return f"elicit_{len(self.asked)}"


class DemoWorkload:
    """A two-session iteration that finishes as soon as it is collected."""

    name = "demo"

    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = list(items or [{"task": "build the thing"}])
        self.collect_calls = 0
        self.ready_after = 1
        self.dispatch_error: OmniError | None = None

    def acquire(self) -> dict[str, Any] | None:
        return self.items.pop(0) if self.items else None

    def dispatch(self, run: Run, omni: Any) -> list[str]:
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return [omni.create_session("ag_worker"), omni.create_session("ag_reviewer")]

    def collect(self, run: Run, omni: Any) -> tuple[bool, dict[str, Any]]:
        self.collect_calls += 1
        if self.collect_calls < self.ready_after:
            return False, {}
        return True, {"tests": "passed", "approval_session_id": run.outstanding[0]}

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:
        return "Ship it?", ["ship", "iterate", "stop"], dict(run.artifacts)

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:
        if decision == "deny":
            return "paused", "owner declined"
        choice = payload.get("choice", "ship")
        if choice == "stop":
            return "completed", "owner stopped the loop"
        return "continue", f"owner chose {choice}"


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    """A store on a real SQLite file, so restarts can be simulated honestly."""
    return Store(tmp_path / "army.db")


def _drive_to_waiting(store: Store, omni: FakeOmni, workload: DemoWorkload) -> Run:
    """Tick until the single run is parked on a human, then return it."""
    supervisor = Supervisor(store, omni, workload)
    for _ in range(10):
        supervisor.tick()
        runs = store.list_runs()
        if runs and runs[0].state is RunState.WAITING_HUMAN:
            return runs[0]
    raise AssertionError("run never reached WAITING_HUMAN")


def test_a_run_walks_the_happy_path(store: Store) -> None:
    """READY through to WAITING_HUMAN, one resting state per tick.

    ``DISPATCHING`` is not a resting state — it is the marker written just
    before sessions are created, so a crash in that window is recognisable as
    "may have dispatched" rather than "never started". A tick therefore passes
    through it and settles in ``COLLECTING``.
    """
    omni, workload = FakeOmni(), DemoWorkload()
    supervisor = Supervisor(store, omni, workload)

    supervisor.tick()
    assert store.list_runs()[0].state is RunState.READY

    supervisor.tick()
    run = store.list_runs()[0]
    assert run.state is RunState.COLLECTING
    assert len(run.outstanding) == 2

    supervisor.tick()
    assert store.list_runs()[0].state is RunState.EVALUATING

    supervisor.tick()
    run = store.list_runs()[0]
    assert run.state is RunState.WAITING_HUMAN
    assert run.approval_id == "elicit_1"
    assert omni.asked[0][1] == "Ship it?"


def test_run_survives_a_restart_and_advances_once(tmp_path: Path) -> None:
    """The acceptance criterion.

    Everything holding the run in memory is thrown away between the question
    and the answer — the store, the supervisor, the client, the workload. Only
    the SQLite file crosses the gap, which is exactly what survives a reboot.
    """
    db = tmp_path / "army.db"
    parked = _drive_to_waiting(Store(db), FakeOmni(), DemoWorkload())
    assert parked.state is RunState.WAITING_HUMAN
    version_when_asked = parked.version

    # The process dies here. Nothing above this line exists any more.
    del parked

    reopened = Store(db)
    supervisor = Supervisor(reopened, FakeOmni(), DemoWorkload(items=[]))
    run = reopened.list_runs()[0]
    assert run.state is RunState.WAITING_HUMAN, "the barrier did not survive"

    supervisor.answer(run.id, CommandKind.APPROVE, {"choice": "ship"})
    supervisor.tick()

    after = reopened.get_run(run.id)
    assert after is not None
    assert after.state is RunState.CONTINUE
    assert after.terminal_reason == "owner chose ship"
    # Exactly one transition: the version moved by one, not by two.
    assert after.version == version_when_asked + 1

    # And ticking again does not move it a second time.
    supervisor.tick()
    settled = reopened.get_run(run.id)
    assert settled is not None
    assert settled.version == after.version


def test_an_answer_given_while_nothing_runs_is_not_lost(tmp_path: Path) -> None:
    """A verdict recorded with no supervisor alive is applied on the next tick."""
    db = tmp_path / "army.db"
    run = _drive_to_waiting(Store(db), FakeOmni(), DemoWorkload())

    # Answer through a store that has no supervisor attached at all.
    offline = Store(db)
    offline.record_command(Command.new(run.id, CommandKind.APPROVE, {"choice": "ship"}, now=1))

    Supervisor(Store(db), FakeOmni(), DemoWorkload(items=[])).tick()

    after = Store(db).get_run(run.id)
    assert after is not None
    assert after.state is RunState.CONTINUE


def test_a_command_is_consumed_exactly_once(store: Store) -> None:
    """Replaying a consumed command must not move the run again."""
    omni, workload = FakeOmni(), DemoWorkload()
    run = _drive_to_waiting(store, omni, workload)
    supervisor = Supervisor(store, omni, workload)

    supervisor.answer(run.id, CommandKind.APPROVE, {"choice": "ship"})
    supervisor.tick()
    assert store.next_command(run.id) is None

    supervisor.tick()
    after = store.get_run(run.id)
    assert after is not None
    assert after.state is RunState.CONTINUE


def test_two_supervisors_racing_produce_one_transition(store: Store) -> None:
    """Compare-and-swap, not last-write-wins."""
    run = store.create_run(Run.new("demo", {}, now=1))
    stale = store.get_run(run.id)
    assert stale is not None

    store.transition(run, RunState.DISPATCHING, now=2)

    with pytest.raises(ConcurrentTransition):
        store.transition(stale, RunState.DISPATCHING, now=2)


def test_an_undefined_move_is_refused(store: Store) -> None:
    """A run must never reach a state no later step knows how to handle."""
    run = store.create_run(Run.new("demo", {}, now=1))

    with pytest.raises(IllegalTransition):
        store.transition(run, RunState.WAITING_HUMAN, now=2)


def test_a_denial_pauses_the_branch(store: Store) -> None:
    """Declining stops this branch; it never auto-approves."""
    omni, workload = FakeOmni(), DemoWorkload()
    run = _drive_to_waiting(store, omni, workload)
    supervisor = Supervisor(store, omni, workload)

    supervisor.answer(run.id, CommandKind.DENY, {})
    supervisor.tick()

    after = store.get_run(run.id)
    assert after is not None
    assert after.state is RunState.PAUSED
    assert after.terminal_reason == "owner declined"


def test_partial_collection_is_persisted(store: Store) -> None:
    """A crash mid-collection costs the unfinished children, not the finished ones."""
    omni = FakeOmni()
    workload = DemoWorkload()
    workload.ready_after = 3
    supervisor = Supervisor(store, omni, workload)

    supervisor.tick()  # open
    supervisor.tick()  # dispatch -> collecting, first collect returns not-done
    run = store.list_runs()[0]

    assert run.state is RunState.COLLECTING
    assert run.outstanding, "children should still be outstanding"


def test_a_transient_dispatch_error_is_retried_not_failed(store: Store) -> None:
    """A server that is briefly down must not burn the iteration."""
    omni = FakeOmni()
    workload = DemoWorkload()
    workload.dispatch_error = OmniError("connection refused")
    supervisor = Supervisor(store, omni, workload)

    supervisor.tick()
    supervisor.tick()

    run = store.list_runs()[0]
    assert run.state is RunState.DISPATCHING
    assert run.terminal_reason is None


def test_a_permanent_dispatch_error_fails_the_run(store: Store) -> None:
    """A malformed request will be malformed next time too."""
    omni = FakeOmni()
    workload = DemoWorkload()
    workload.dispatch_error = OmniError("bad request", 400)
    supervisor = Supervisor(store, omni, workload)

    supervisor.tick()
    supervisor.tick()

    run = store.list_runs()[0]
    assert run.state is RunState.FAILED
    assert "dispatch failed" in (run.terminal_reason or "")


def test_a_run_stops_retrying_eventually(store: Store) -> None:
    """Spinning on a doomed iteration burns the quota the rest of the loop needs."""
    run = store.create_run(Run.new("demo", {}, now=1))
    run = store.transition(run, RunState.DISPATCHING, attempt=MAX_ATTEMPTS, now=2)
    run = store.transition(run, RunState.FAILED, terminal_reason="x", now=3)

    supervisor = Supervisor(store, FakeOmni(), DemoWorkload(items=[]))
    supervisor.tick()

    after = store.get_run(run.id)
    assert after is not None
    assert after.state is RunState.FAILED


def test_a_full_lane_holds_the_run_rather_than_failing_it(store: Store) -> None:
    """Backpressure parks work; it does not throw it away."""
    lanes = Lanes([Lane("claude-native", max_concurrent=1)])
    lanes.acquire(1, "claude-native")
    supervisor = Supervisor(store, FakeOmni(), DemoWorkload(), lanes=lanes)

    supervisor.tick()
    supervisor.tick()

    run = store.list_runs()[0]
    assert run.state is RunState.READY
    assert run.attempt == 0, "a run held by backpressure has not attempted anything"


def test_a_stuck_run_is_eventually_failed(store: Store) -> None:
    """A transient error retried forever looks like progress from outside.

    The loop keeps ticking, the report says "unchanged", and nothing ever
    happens — which is worse than failing, because nobody goes looking.
    """
    from army.supervisor import STALL_SECONDS

    omni, workload = FakeOmni(), DemoWorkload()
    workload.ready_after = 10_000  # never finishes collecting
    supervisor = Supervisor(store, omni, workload)
    supervisor.tick(now=1_000)
    supervisor.tick(now=1_000)
    assert store.list_runs()[0].state is RunState.COLLECTING

    supervisor.tick(now=1_000 + STALL_SECONDS + 1)

    run = store.list_runs()[0]
    assert run.state is RunState.FAILED
    assert "stuck in collecting" in (run.terminal_reason or "")


def test_waiting_on_a_human_is_never_stuck(store: Store) -> None:
    """Waiting overnight is the design, not a stall.

    D8's whole point is that the loop waits for an answer that may come the
    next morning. A stall guard that fails those is a stall guard that breaks
    the feature it was added to protect.
    """
    from army.supervisor import STALL_SECONDS

    omni, workload = FakeOmni(), DemoWorkload()
    run = _drive_to_waiting(store, omni, workload)
    supervisor = Supervisor(store, omni, workload)

    supervisor.tick(now=run.updated_at + STALL_SECONDS * 24)

    after = store.get_run(run.id)
    assert after is not None
    assert after.state is RunState.WAITING_HUMAN
