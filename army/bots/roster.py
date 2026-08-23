"""The read model: what the fleet is doing, assembled at read time.

Nine operational statuses, none of them stored. They are computed from the runs,
the vendor gates and the clock every time somebody looks, which is what makes
it impossible for the roster to disagree with the runs it describes. Storing
them would give the system a second state machine, and the one that goes stale
is always the one on the screen.

The question this exists to answer, in one glance: **which one is stuck?**
"""

from __future__ import annotations

from dataclasses import dataclass

from army.bots.model import Bot, BotStatus, DerivedStatus, derive_status
from army.bots.store import BotStore

#: Statuses that mean a person has to do something. Everything else is the
#: fleet running itself.
NEEDS_A_HUMAN: frozenset[DerivedStatus] = frozenset(
    {DerivedStatus.WAITING_HUMAN, DerivedStatus.BLOCKED}
)


@dataclass(frozen=True)
class RosterEntry:
    """One bot, with everything needed to render a line about it.

    :param bot: The bot itself.
    :param status: What it is doing, derived.
    :param live_run_id: The run in flight, when there is one.
    :param blocked_until: When its vendor lane reopens, when that is what is
        holding it.
    """

    bot: Bot
    status: DerivedStatus
    live_run_id: str | None = None
    blocked_until: int | None = None

    @property
    def needs_a_human(self) -> bool:
        """Whether this row is the operator's problem rather than the fleet's."""
        return self.status in NEEDS_A_HUMAN

    def due_in(self, now: int) -> int | None:
        """
        Seconds until the next wake, or ``None`` when nothing is scheduled.

        :param now: Epoch seconds.
        :returns: A non-negative delay, or ``None``.
        """
        if self.bot.next_due_at is None:
            return None
        return max(0, self.bot.next_due_at - now)


def roster(bots: BotStore, *, now: int, status: BotStatus | None = None) -> list[RosterEntry]:
    """
    Assemble the fleet view in three queries, whatever its size.

    Three, not three-per-bot: the live runs and the blocked vendors are each
    read once and joined in memory. A roster that issued a query per bot would
    be the thing that makes ``army bots`` slow exactly when the fleet is busy.

    :param bots: The bot store.
    :param now: Epoch seconds.
    :param status: Restrict to one lifecycle state, or ``None`` for all.
    :returns: One entry per bot, ordered with the ones needing a person first,
        then by how soon they wake — which is the order an operator reads in.
    """
    everyone = bots.list(status=status)
    live = bots.live_run_states()
    live_ids = bots.live_run_ids()
    blocked = bots.blocked_vendors(now=now)

    entries = [
        RosterEntry(
            bot=bot,
            status=derive_status(
                bot,
                live_run_state=live.get(bot.id),
                lane_blocked=bot.harness is not None and bot.harness in blocked,
                now=now,
            ),
            live_run_id=live_ids.get(bot.id),
            blocked_until=blocked.get(bot.harness) if bot.harness else None,
        )
        for bot in everyone
    ]
    return sorted(entries, key=lambda entry: _reading_order(entry, now))


def _reading_order(entry: RosterEntry, now: int) -> tuple[int, int, str]:
    """
    Sort key putting what needs a person first and the far future last.

    :param entry: The row.
    :param now: Epoch seconds.
    :returns: ``(band, delay, slug)``.
    """
    if entry.needs_a_human:
        band = 0
    elif entry.status is DerivedStatus.RUNNING:
        band = 1
    elif entry.status is DerivedStatus.INACTIVE:
        band = 4
    elif entry.bot.next_due_at is None:
        band = 3
    else:
        band = 2
    delay = entry.due_in(now)
    return (band, delay if delay is not None else 0, entry.bot.slug)


def summarise(entries: list[RosterEntry]) -> dict[str, int]:
    """
    Count the fleet by derived status, for a one-line header.

    :param entries: The roster.
    :returns: ``{status: count}`` for the statuses actually present, so an
        empty bucket never appears as a zero somebody has to read past.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.status.value] = counts.get(entry.status.value, 0) + 1
    return counts
