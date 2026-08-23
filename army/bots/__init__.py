"""Bot mode: long-running, named, mission-driven bots over the ``army`` loop.

A Bot is data. A body is disposable.

A bot owns no process between iterations — it owns rows. The supervisor tick
reads which bots are due, spawns a harness session for exactly one run, and
releases it. Whether the previous tick ended by returning, by being killed, or
by the machine losing power makes no difference to the next one.

This package adds to :mod:`army` and is never imported by it. That direction is
deliberate: the run state machine has to stay comprehensible on its own, and a
dependency pointing the other way would make "what owns run state" ambiguous
again.
"""

from army.bots.model import (
    Bot,
    BotRevision,
    BotStatus,
    DerivedStatus,
    RunOutcome,
    WakeKind,
    WakePolicy,
    WakeReason,
)
from army.bots.schedule import Wake, first_wake, next_wake

__all__ = [
    "Bot",
    "BotRevision",
    "BotStatus",
    "DerivedStatus",
    "RunOutcome",
    "Wake",
    "WakeKind",
    "WakePolicy",
    "WakeReason",
    "first_wake",
    "next_wake",
]
