"""When a bot wakes next, computed from how its last iteration went.

The whole scheduler is one function — :func:`next_wake` — and it is pure. Given
a policy, an outcome and a clock it returns the next due time and the streak
that goes with it. No clock reads, no database, no randomness that a test
cannot pin.

The rule that matters most is the one that is easy to get backwards:
**continuous is not zero-delay scheduling.** A bot that finds nothing to do
must get further away from the vendor each time, not closer, or the loop is a
quota feedback loop wearing a scheduler's clothes.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime

from dateutil.rrule import rrulestr

from army.bots.model import (
    InvalidWakePolicy,
    RunOutcome,
    WakeKind,
    WakePolicy,
    WakeReason,
)

#: How long a transient failure waits before the next attempt. Short, because
#: the thing that bounds an error loop is ``bots.error_streak``, not the delay.
RETRY_BASE_S = 30

#: Ceiling on the error backoff, so a bot failing all night is not also
#: hammering all night.
RETRY_MAX_S = 900

#: Consecutive failing iterations before a bot stops scheduling itself and
#: waits for a person. ``Run.attempt`` resets every run, so without this a bot
#: that fails cleanly each time retries forever.
MAX_ERROR_STREAK = 5

#: Consecutive empty iterations before a bot is paused and reported. Backoff
#: caps the *rate*; this caps the *pointlessness*. A mission with nothing in it
#: for this many wakes is a mission that needs a person to look at it.
MAX_IDLE_STREAK = 12


@dataclass(frozen=True)
class Wake:
    """The scheduler's answer for one bot.

    :param next_due_at: Epoch seconds of the next wake, or ``None`` when
        nothing is scheduling this bot and only an insert will move it.
    :param reason: What set the time, so the derived status can tell a blocked
        bot from a merely quiet one.
    :param idle_streak: Consecutive empty iterations after this outcome.
    :param error_streak: Consecutive failing iterations after this outcome.
    :param exhausted: Whether a streak cap was reached, so the caller should
        pause the bot and report rather than schedule it again.
    """

    next_due_at: int | None
    reason: WakeReason | None
    idle_streak: int
    error_streak: int
    exhausted: bool = False


def next_wake(
    policy: WakePolicy,
    outcome: RunOutcome,
    *,
    now: int,
    idle_streak: int = 0,
    error_streak: int = 0,
    rng: random.Random | None = None,
) -> Wake:
    """
    Decide when a bot should wake after an iteration reported *outcome*.

    :param policy: The bot's wake policy.
    :param outcome: How the iteration classified itself.
    :param now: Epoch seconds.
    :param idle_streak: Consecutive empty iterations before this one.
    :param error_streak: Consecutive failures before this one.
    :param rng: Source of jitter; defaults to the module's own. Pass a seeded
        :class:`random.Random` in tests.
    :returns: The next wake.
    """
    if outcome is RunOutcome.BLOCKED:
        # No next wake at all. A bot waiting on a person costs exactly nothing
        # until the verdict lands, and the verdict's own transaction is what
        # sets a time again. Streaks are preserved: being blocked is not
        # evidence about whether there was work.
        return Wake(None, WakeReason.HUMAN, idle_streak, error_streak)

    if outcome is RunOutcome.RATE_LIMITED:
        # The vendor gate is shared, so the bot is not individually punished
        # for a limit that applies to everyone on that lane. But it does not
        # come back *immediately* either: the gate is only written when the run
        # identified its vendor, and a bot that trusted a gate that was never
        # written would redispatch every tick against the vendor that just said
        # stop — the quota spin this whole model exists to prevent. So the
        # floor holds even when the gate does not.
        return Wake(
            now + _jittered(RETRY_BASE_S, policy.jitter, rng, floor=1),
            WakeReason.BACKOFF,
            idle_streak,
            error_streak,
        )

    if outcome is RunOutcome.RETRYABLE_ERROR:
        streak = error_streak + 1
        if streak >= MAX_ERROR_STREAK:
            return Wake(None, WakeReason.HUMAN, idle_streak, streak, exhausted=True)
        delay = _capped(RETRY_BASE_S, streak - 1, policy.factor, RETRY_MAX_S)
        return Wake(
            now + _jittered(delay, policy.jitter, rng, floor=RETRY_BASE_S),
            WakeReason.BACKOFF,
            idle_streak,
            streak,
        )

    if outcome is RunOutcome.NO_WORK:
        streak = idle_streak + 1
        # The cap applies to every kind. Gating it on `continuous` let a
        # `FREQ=MINUTELY` bot with a permanently false precondition poll at full
        # rate forever, which is the same quota loop wearing a schedule.
        if streak >= MAX_IDLE_STREAK:
            return Wake(None, WakeReason.HUMAN, streak, error_streak, exhausted=True)
        # The error streak is *preserved*, not reset. A bot whose vendor call
        # always fails but whose precondition usually says "empty" would
        # otherwise alternate error(+1) / idle(reset) and never reach the error
        # cap — paying for a failing vendor turn forever without ever alarming.
        return _scheduled(policy, now, idle_streak=streak, error_streak=error_streak, rng=rng)

    # WORK_DONE. Both streaks reset: the mission produced something, so neither
    # "nothing to do" nor "keeps failing" is true any more.
    return _scheduled(policy, now, idle_streak=0, error_streak=0, rng=rng)


def _scheduled(
    policy: WakePolicy,
    now: int,
    *,
    idle_streak: int,
    error_streak: int,
    rng: random.Random | None,
) -> Wake:
    """Apply the policy's own rule, given streaks already updated."""
    if policy.kind is WakeKind.RRULE:
        following = next_occurrence(policy.rrule or "", now, anchor=policy.anchor)
        if following is None:
            # A finite rule that has run out. Writing Wake(None, SCHEDULE) would
            # leave an ACTIVE bot with no wake and no reason, which the stalled
            # scan then reports as a lost succession every tick forever — for a
            # bot that simply finished its schedule.
            return Wake(None, WakeReason.SCHEDULE, idle_streak, error_streak, exhausted=True)
        # A floor under the rule itself. `FREQ=SECONDLY` parses fine and would
        # otherwise be honoured literally.
        return Wake(
            max(following, now + policy.min_interval_s),
            WakeReason.SCHEDULE,
            idle_streak,
            error_streak,
        )
    if policy.kind is WakeKind.CONTINUOUS:
        if idle_streak == 0:
            return Wake(now + policy.min_interval_s, WakeReason.SCHEDULE, 0, error_streak)
        delay = _capped(policy.base_s, idle_streak - 1, policy.factor, policy.max_s)
        floor = max(delay, policy.min_interval_s)
        return Wake(
            now + _jittered(floor, policy.jitter, rng, floor=policy.min_interval_s),
            WakeReason.BACKOFF,
            idle_streak,
            error_streak,
        )
    if policy.kind is WakeKind.ON_MESSAGE:
        return Wake(None, WakeReason.EVENT, idle_streak, error_streak)
    return Wake(None, WakeReason.MANUAL, idle_streak, error_streak)


def first_wake(policy: WakePolicy, *, now: int) -> Wake:
    """
    The wake a bot gets when a human activates it.

    An rrule bot waits for its next occurrence; a continuous bot is due at once
    so activation visibly does something; event and manual bots wait to be
    poked.

    :param policy: The bot's wake policy.
    :param now: Epoch seconds.
    :returns: The initial wake.
    """
    if policy.kind is WakeKind.RRULE:
        following = next_occurrence(policy.rrule or "", now, anchor=policy.anchor)
        if following is None:
            # Activating a bot whose schedule has already run out would make an
            # ACTIVE bot that can never run. Say so at activation.
            return Wake(None, WakeReason.SCHEDULE, 0, 0, exhausted=True)
        return Wake(following, WakeReason.SCHEDULE, 0, 0)
    if policy.kind is WakeKind.CONTINUOUS:
        return Wake(now, WakeReason.SCHEDULE, 0, 0)
    if policy.kind is WakeKind.ON_MESSAGE:
        return Wake(None, WakeReason.EVENT, 0, 0)
    return Wake(None, WakeReason.MANUAL, 0, 0)


def next_occurrence(rule: str, now: int, *, anchor: int | None = None) -> int | None:
    """
    The first time this rule fires strictly after *now*.

    Missed occurrences coalesce by construction: asking for the next one after
    the current clock skips everything the downtime swallowed, so a daily bot
    that was off for a week fires once rather than seven times. Replaying a
    backlog of wakes is the failure this avoids.

    **The anchor is what stops the schedule drifting.** ``dtstart`` is the
    phase of the recurrence, so anchoring it to *now* re-phases the rule on
    every evaluation: a bot due at 09:00 that takes twenty minutes settles at
    09:20, and "daily" then means 09:20 tomorrow, 09:41 the day after, and so
    on forever. Anchoring to a fixed instant chosen once — activation — keeps
    "daily at nine" meaning nine.

    :param rule: An iCalendar ``RRULE`` string, e.g. ``"FREQ=DAILY;BYHOUR=9"``.
    :param now: Epoch seconds.
    :param anchor: Epoch seconds the recurrence is phased from. ``None``
        falls back to *now*, which is right only for a rule whose ``BY`` parts
        already pin an absolute time of day.
    :returns: Epoch seconds of the next occurrence, or ``None`` for a finite
        rule that has run out.
    :raises InvalidWakePolicy: If the rule or the clock cannot be used.
    """
    try:
        # Inside the guard: a caller that passes milliseconds — the classic
        # timestamp bug — raises here rather than killing the tick.
        start = datetime.fromtimestamp(now, tz=UTC)
        phase = datetime.fromtimestamp(anchor, tz=UTC) if anchor is not None else start
        occurrences = rrulestr(rule, dtstart=phase)
        following = occurrences.after(start, inc=False)
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise InvalidWakePolicy(f"cannot evaluate rrule {rule!r} at {now}: {exc}") from exc
    if following is None:
        return None
    return int(following.timestamp())


def _capped(base: int, exponent: int, factor: float, ceiling: int) -> int:
    """
    Exponential growth that stops at a ceiling, without overflowing on the way.

    A long streak makes ``factor ** exponent`` enormous, and computing it just
    to throw it away is how a scheduler becomes the slow part of a tick.

    :param base: First step.
    :param exponent: How many doublings.
    :param factor: Multiplier per step.
    :param ceiling: Longest delay allowed.
    :returns: The delay in seconds.
    """
    delay = float(base)
    for _ in range(max(0, exponent)):
        delay *= factor
        if delay >= ceiling:
            return ceiling
    return int(min(delay, ceiling))


def _jittered(delay: int, jitter: float, rng: random.Random | None, *, floor: int = 1) -> int:
    """
    Spread a delay so a fleet that went idle together does not wake together.

    Jitter subtracts rather than adds, which is the only way both promises can
    hold at once. Adding meant the computed ceiling was not a ceiling — a
    documented "15 minute" error cap became half an hour with jitter 1.0 — and
    clamping the sum back to the cap removed the spread exactly where it is
    most needed, with a whole fleet sitting at the maximum.

    Subtracting keeps the cap true and still scatters, at the cost of waking
    slightly sooner than the nominal delay. That is bounded by *floor*, which
    is what actually protects the vendor.

    :param delay: The computed delay in seconds, already capped.
    :param jitter: Fraction of *delay* to spread over.
    :param rng: Source of randomness, or ``None`` for the module's own.
    :param floor: The shortest delay this may return.
    :returns: A delay in ``[floor, delay]``.
    """
    if jitter <= 0:
        return max(delay, floor)
    source = rng if rng is not None else _RNG
    spread = delay - int(source.random() * jitter * delay)
    return max(spread, floor)


#: Module-level source so callers need not thread one through. Tests pass their
#: own seeded generator instead of monkeypatching this.
_RNG = random.Random()
