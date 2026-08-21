"""The workflow state machine: what a run is, and which moves are legal.

One :class:`Run` is one iteration of the loop — plan, dispatch, collect,
evaluate, ask, continue. Its :class:`RunState` is the program counter that
Omnigent has nowhere to keep, and ``version`` is the fencing token that makes
advancing it safe: every transition is a compare-and-swap against the version
it was read at, so two supervisors racing on the same run produce exactly one
move and the loser is told it lost.

A verdict from a human is a :class:`Command` — a row, not a callback. That is
the whole reason this package exists: a parked coroutine dies with the process
that owned it, and cannot be revived, but a row that says "the answer was yes"
is still there afterwards and still means the same thing.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RunState(str, Enum):
    """Where one iteration has got to.

    The first five are working states; the last four are terminal for this
    iteration. ``CONTINUE`` means the loop should start another iteration,
    which is different from ``COMPLETED`` (the work is done) and from
    ``PAUSED`` (a human stopped this branch and only a human restarts it).
    """

    READY = "ready"
    DISPATCHING = "dispatching"
    COLLECTING = "collecting"
    EVALUATING = "evaluating"
    WAITING_HUMAN = "waiting_human"
    CONTINUE = "continue"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


#: States the supervisor does not advance on its own. ``PAUSED`` is here
#: because only a human restarts it — but a ``RESUME`` command does exactly
#: that, so it is resting rather than final.
TERMINAL: frozenset[RunState] = frozenset(
    {RunState.CONTINUE, RunState.PAUSED, RunState.COMPLETED, RunState.FAILED}
)

#: The one state a human can bring a run back from. Everything else in
#: :data:`TERMINAL` is genuinely final: a completed run is done, a failed one
#: needs a new iteration rather than a resurrection, and ``CONTINUE`` has
#: already handed off to the next.
RESUMABLE: frozenset[RunState] = frozenset({RunState.PAUSED})

#: The moves the supervisor may make. Anything not listed is a bug, and
#: :func:`assert_legal` says so rather than letting a run reach a state no
#: later step knows how to handle.
LEGAL: dict[RunState, frozenset[RunState]] = {
    RunState.READY: frozenset({RunState.DISPATCHING, RunState.COMPLETED, RunState.FAILED}),
    RunState.DISPATCHING: frozenset({RunState.COLLECTING, RunState.FAILED}),
    # READY is the quota exit: a vendor that refused on subscription limit
    # produced no work to evaluate and nothing that failed, so the iteration
    # goes back to the start of the queue and waits for the lane to wake. Same
    # reasoning as PAUSED below — what it was collecting is gone, so
    # re-dispatching is the only honest move.
    RunState.COLLECTING: frozenset(
        {RunState.COLLECTING, RunState.EVALUATING, RunState.READY, RunState.FAILED}
    ),
    RunState.EVALUATING: frozenset({RunState.WAITING_HUMAN, RunState.FAILED}),
    RunState.WAITING_HUMAN: frozenset(
        {RunState.CONTINUE, RunState.PAUSED, RunState.COMPLETED, RunState.FAILED}
    ),
    RunState.CONTINUE: frozenset(),
    # A paused branch goes back to the start of an iteration, not to wherever
    # it was: the sessions it was collecting are long gone by the time someone
    # resumes, so re-dispatching is the only honest move.
    RunState.PAUSED: frozenset({RunState.READY}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
}


class IllegalTransition(RuntimeError):
    """Raised when a move is not in :data:`LEGAL`."""


def assert_legal(source: RunState, target: RunState) -> None:
    """
    Reject a move the state machine does not define.

    :param source: State the run is in now.
    :param target: State it would move to.
    :raises IllegalTransition: If the move is not declared in :data:`LEGAL`.
    """
    if target not in LEGAL[source]:
        raise IllegalTransition(f"{source.value} -> {target.value} is not a legal move")


class CommandKind(str, Enum):
    """What a durable command asks the supervisor to do."""

    APPROVE = "approve"
    DENY = "deny"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


@dataclass
class Run:
    """One iteration of the loop.

    :param id: Stable run id.
    :param workflow: Which loop definition this is an iteration of.
    :param state: The program counter.
    :param version: Monotonic fencing token, bumped on every transition. A
        transition supplies the version it read; a mismatch means someone else
        moved first.
    :param attempt: How many times this iteration has been dispatched. Used to
        stop a run that keeps failing from re-dispatching forever.
    :param created_at: Unix epoch seconds the run was opened.
    :param updated_at: Unix epoch seconds of the last transition.
    :param payload: The work item this iteration is about — opaque to the
        engine, meaningful to the workload adapter.
    :param artifacts: Evidence accumulated so far, keyed by producer. This is
        what the human is shown when the run reaches ``WAITING_HUMAN``.
    :param outstanding: Omnigent session ids dispatched and not yet collected.
    :param approval_id: The Omnigent elicitation id this run is parked on, when
        it is in ``WAITING_HUMAN``.
    :param terminal_reason: Why the run ended, for a terminal state.
    """

    id: str
    workflow: str
    state: RunState
    version: int
    attempt: int
    created_at: int
    updated_at: int
    payload: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    outstanding: list[str] = field(default_factory=list)
    approval_id: str | None = None
    terminal_reason: str | None = None

    @staticmethod
    def new(workflow: str, payload: dict[str, Any], *, now: int) -> Run:
        """
        Open a fresh run in :attr:`RunState.READY`.

        :param workflow: Loop definition name.
        :param payload: The work item.
        :param now: Unix epoch seconds, supplied by the caller so tests and
            replays can pin it.
        :returns: The new run, not yet persisted.
        """
        return Run(
            id=uuid.uuid4().hex,
            workflow=workflow,
            state=RunState.READY,
            version=0,
            attempt=0,
            created_at=now,
            updated_at=now,
            payload=payload,
        )

    @property
    def is_terminal(self) -> bool:
        """Whether the supervisor is finished with this run."""
        return self.state in TERMINAL


@dataclass(frozen=True)
class Command:
    """A durable instruction to advance a run.

    Commands exist so an answer can outlive the process that asked the
    question. They are consumed exactly once: the supervisor marks a command
    consumed in the same transaction as the transition it caused, so a crash
    between the two replays the command rather than losing it.

    :param id: Stable command id.
    :param run_id: Run the command applies to.
    :param kind: What it asks for.
    :param payload: Anything the answer carried, e.g. the option chosen from a
        multi-select approval.
    :param created_at: Unix epoch seconds the command was recorded.
    :param consumed_at: Unix epoch seconds it was applied, or ``None`` while
        still outstanding.
    """

    id: str
    run_id: str
    kind: CommandKind
    payload: dict[str, Any]
    created_at: int
    consumed_at: int | None = None

    @staticmethod
    def new(run_id: str, kind: CommandKind, payload: dict[str, Any], *, now: int) -> Command:
        """
        Record a new, unconsumed command.

        :param run_id: Run the command applies to.
        :param kind: What it asks for.
        :param payload: Anything the answer carried.
        :param now: Unix epoch seconds.
        :returns: The new command, not yet persisted.
        """
        return Command(
            id=uuid.uuid4().hex,
            run_id=run_id,
            kind=kind,
            payload=payload,
            created_at=now,
        )


def dumps(value: Any) -> str:
    """Encode a JSON column with stable key order so rows diff cleanly."""
    return json.dumps(value, sort_keys=True)


def loads(raw: str | None, fallback: Any) -> Any:
    """
    Decode a JSON column, falling back when it is absent or corrupt.

    A single unreadable column must not make a whole run unloadable — the run
    is what holds the loop's place, and losing it is worse than losing one
    artifact blob.

    :param raw: The stored text, or ``None``.
    :param fallback: What to return when there is nothing usable.
    :returns: The decoded value, or *fallback*.
    """
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback
