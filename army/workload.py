"""The seam between the generic loop and a specific job of work.

The engine knows about runs, states, versions and approvals. It knows nothing
about what the work *is* — no hypotheses, no backtests, no repositories. All of
that lives behind :class:`Workload`, which is the only place a domain gets to
speak.

Keeping the vocabulary on this side of the seam is what lets the engine, the
agent YAML and anything upstreamed stay generic: if a change to the engine
starts needing a domain word, the change belongs in a workload instead.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from army.omni import OmniClient
from army.state import Run


@runtime_checkable
class Workload(Protocol):
    """One job of work the loop can run iterations of.

    Every method is called at most once per supervisor tick, and may be called
    again after a restart with the same run, so all five must be safe to
    repeat. Where that is not naturally true — anything with an outside effect
    — go through the effects journal rather than doing it here.
    """

    name: str

    def acquire(self) -> dict[str, Any] | None:
        """
        Take the next work item, or report that there is nothing to do.

        :returns: The item, opaque to the engine, or ``None`` when the queue is
            empty. Returning ``None`` is normal and not an error — a loop with
            nothing to do should idle, not fail.
        """
        ...

    def dispatch(self, run: Run, omni: OmniClient) -> list[str]:
        """
        Start the sessions this iteration needs.

        :param run: The run, in ``DISPATCHING``.
        :param omni: Client for creating and messaging sessions.
        :returns: Session ids to collect from. An empty list means the
            iteration needs no agent work and goes straight to evaluation.
        """
        ...

    def collect(self, run: Run, omni: OmniClient) -> tuple[bool, dict[str, Any]]:
        """
        Check whether the dispatched work has finished, and gather what it produced.

        :param run: The run, in ``COLLECTING``.
        :param omni: Client for reading session state.
        :returns: ``(done, artifacts)``. While *done* is ``False`` the run
            stays in ``COLLECTING`` and is asked again on the next tick.
        """
        ...

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:
        """
        Turn the collected evidence into the question to put to the human.

        Benchmarks, gates and test results belong in the evidence, not in the
        decision: they are inputs to the question, never a substitute for
        asking it.

        :param run: The run, in ``EVALUATING``.
        :returns: ``(question, options, evidence)``.
        """
        ...

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:
        """
        Interpret the answer.

        :param run: The run, in ``WAITING_HUMAN``.
        :param decision: The command kind that arrived, e.g. ``"approve"``.
        :param payload: Anything the answer carried, e.g. the option chosen.
        :returns: ``(next_state, reason)`` where *next_state* is one of
            ``"continue"``, ``"paused"``, ``"completed"`` or ``"failed"``.
        """
        ...
