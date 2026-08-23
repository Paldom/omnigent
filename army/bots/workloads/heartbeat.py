"""A bot that does nothing, so you can watch the loop do everything.

No sessions, no vendor, no subscription. It exists so the first thing a person
runs after installing Bot mode exercises every load-bearing mechanic — the
migration, the transaction, the partial index, the outcome policy, the derived
status — and none of the product surface. If this does not loop, nothing else
is worth debugging yet.

It is also the honest test of the claim the whole design rests on: a bot that
holds no process between iterations. Kill the supervisor mid-loop and start it
again; the heartbeat carries on from its rows.
"""

from __future__ import annotations

import time
from typing import Any

from army.bots.model import RunOutcome
from army.omni import OmniClient
from army.state import Run


class HeartbeatWorkload:
    """An iteration that finishes immediately and reports what it was told to.

    :param outcome: What every iteration reports. ``work_done`` keeps the bot
        at its floor; ``no_work`` makes it visibly back off, which is the more
        interesting thing to watch.
    :param ask: Whether to put a question to the human each iteration. Left on,
        because "every iteration ends in an approval" is the settled rule and a
        demo that skips it teaches the wrong shape.
    """

    name = "heartbeat"

    #: The choices offered at the barrier.
    OPTIONS: tuple[str, ...] = ("continue", "stop")

    def __init__(self, outcome: str = "work_done", ask: bool = True) -> None:
        self.outcome = RunOutcome(outcome).value
        self.ask = ask
        self.beats = 0

    def acquire(self) -> dict[str, Any] | None:
        """Always has an iteration to offer; the precondition is what gates it."""
        self.beats += 1
        return {"beat": self.beats, "at": int(time.time())}

    def dispatch(self, run: Run, omni: OmniClient) -> list[str]:  # noqa: ARG002
        """Start nothing. An empty list means the iteration needs no agent work."""
        return []

    def collect(self, run: Run, omni: OmniClient) -> tuple[bool, dict[str, Any]]:  # noqa: ARG002
        """Finish at once, declaring the outcome the scheduler will act on."""
        return True, {"outcome": self.outcome, "beat": run.payload.get("beat")}

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:
        """Ask, so the run reaches ``WAITING_HUMAN`` like a real one."""
        beat = run.payload.get("beat")
        return (
            f"Heartbeat {beat} finished. Keep going?",
            list(self.OPTIONS),
            {"beat": beat, "outcome": self.outcome},
        )

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:  # noqa: ARG002
        """Continue unless told to stop; a decline pauses this branch."""
        if decision == "deny":
            return "paused", "declined; paused until resumed"
        if payload.get("choice") == "stop":
            return "completed", "owner stopped the heartbeat"
        return "continue", "beat acknowledged"
