"""The wake policy: what an outcome costs, and what it buys.

The single most important property in this file is that a bot which keeps
finding nothing to do gets *further* from the vendor each time. Continuous is
not zero-delay scheduling; a scheduler that treats it that way is a quota
feedback loop, and the whole outcome model exists to prevent exactly that.
"""

from __future__ import annotations

import random

import pytest

from army.bots.model import (
    InvalidWakePolicy,
    RunOutcome,
    WakeKind,
    WakePolicy,
    WakeReason,
)
from army.bots.schedule import (
    MAX_ERROR_STREAK,
    MAX_IDLE_STREAK,
    first_wake,
    next_occurrence,
    next_wake,
)

NOW = 1_700_000_000

#: Seeded so jitter is real but reproducible. Pinning the generator rather than
#: setting jitter to zero keeps the jitter path itself under test.
RNG = random.Random(20260823)


def _continuous(**kwargs: object) -> WakePolicy:
    return WakePolicy(kind=WakeKind.CONTINUOUS, precondition="always", **kwargs)  # type: ignore[arg-type]


def test_a_continuous_bot_with_work_wakes_at_its_floor() -> None:
    wake = next_wake(_continuous(min_interval_s=90), RunOutcome.WORK_DONE, now=NOW, rng=RNG)
    assert wake.next_due_at == NOW + 90
    assert wake.idle_streak == 0
    assert wake.reason is WakeReason.SCHEDULE


def _idle_delays(policy: WakePolicy, *, rng: random.Random) -> list[int]:
    """The delay after each consecutive empty iteration, up to the streak cap."""
    delays: list[int] = []
    streak = 0
    for _ in range(MAX_IDLE_STREAK - 1):
        wake = next_wake(
            policy, RunOutcome.NO_WORK, now=NOW, idle_streak=streak, error_streak=0, rng=rng
        )
        assert wake.next_due_at is not None
        delays.append(wake.next_due_at - NOW)
        streak = wake.idle_streak
    return delays


def test_an_idle_bot_backs_off_monotonically_until_it_reaches_the_ceiling() -> None:
    """The load-bearing property: each empty iteration is further from the last.

    Measured without jitter, because jitter is a spread and the growth is the
    claim. Once the ceiling is reached the sequence stops growing, which is the
    other half of the contract and is asserted separately below.
    """
    policy = _continuous(min_interval_s=60, base_s=60, max_s=3600, factor=2.0, jitter=0.0)
    delays = _idle_delays(policy, rng=RNG)

    growing = delays[: delays.index(3600) + 1]
    assert growing == sorted(growing), f"backoff went backwards: {growing}"
    assert len(set(growing)) == len(growing), "backoff repeated a delay before the ceiling"
    assert delays[0] == 60
    assert all(delay == 3600 for delay in delays[len(growing) :])


def test_jitter_spreads_a_fleet_sitting_at_the_ceiling(monkeypatch) -> None:
    """The case jitter exists for, and the one an additive spread lost.

    Adding jitter then clamping to the cap gave every bot at the maximum the
    identical delay — no spread at all, precisely when a whole fleet has
    backed off together. Subtracting keeps the cap honest and still scatters.
    """
    policy = _continuous(min_interval_s=60, base_s=60, max_s=3600, factor=2.0, jitter=0.2)
    at_ceiling = {_idle_delays(policy, rng=random.Random(seed))[-1] for seed in range(20)}
    assert len(at_ceiling) > 1, "jitter did not spread bots sitting at the ceiling"
    assert max(at_ceiling) <= 3600, "jitter pushed a delay past its documented cap"
    assert min(at_ceiling) >= 60


def test_backoff_never_dips_below_the_floor() -> None:
    """A base below ``min_interval_s`` must not let an idle bot poll faster."""
    policy = _continuous(min_interval_s=600, base_s=10, max_s=3600)
    wake = next_wake(policy, RunOutcome.NO_WORK, now=NOW, idle_streak=0, rng=RNG)
    assert wake.next_due_at is not None
    assert wake.next_due_at - NOW >= 600


def test_jitter_never_pulls_a_wake_below_the_floor() -> None:
    """Jitter scatters, but the floor is what actually protects the vendor."""
    policy = _continuous(min_interval_s=100, base_s=400, max_s=400, jitter=0.9)
    for seed in range(50):
        wake = next_wake(
            policy, RunOutcome.NO_WORK, now=NOW, idle_streak=1, rng=random.Random(seed)
        )
        assert wake.next_due_at is not None
        assert NOW + 100 <= wake.next_due_at <= NOW + 400


def test_a_bot_that_is_never_busy_is_eventually_paused_rather_than_spun() -> None:
    """Backoff caps the rate; the streak cap catches an impossible mission."""
    wake = next_wake(
        _continuous(), RunOutcome.NO_WORK, now=NOW, idle_streak=MAX_IDLE_STREAK - 1, rng=RNG
    )
    assert wake.exhausted
    assert wake.next_due_at is None


def test_blocked_has_no_next_wake_and_preserves_both_streaks() -> None:
    """Waiting on a person says nothing about whether there was work."""
    wake = next_wake(
        _continuous(), RunOutcome.BLOCKED, now=NOW, idle_streak=3, error_streak=2, rng=RNG
    )
    assert wake.next_due_at is None
    assert wake.reason is WakeReason.HUMAN
    assert (wake.idle_streak, wake.error_streak) == (3, 2)


def test_rate_limited_backs_off_even_when_no_gate_was_written() -> None:
    """The floor holds without the gate, because the gate is not guaranteed.

    Returning "due now" and trusting the shared vendor gate is only safe if a
    gate was actually written — and it is not, for a run whose vendor could not
    be identified, or a bot with no harness pinned. Trusting it meant
    redispatching every tick against the vendor that had just said stop.

    The streaks are still preserved: a vendor limit says nothing about whether
    the mission had work or whether the bot is broken.
    """
    wake = next_wake(_continuous(), RunOutcome.RATE_LIMITED, now=NOW, idle_streak=1, rng=RNG)
    assert wake.next_due_at is not None
    assert wake.next_due_at > NOW
    assert wake.idle_streak == 1
    assert wake.error_streak == 0


def test_errors_retry_briefly_then_stop() -> None:
    wake = next_wake(_continuous(), RunOutcome.RETRYABLE_ERROR, now=NOW, error_streak=0, rng=RNG)
    assert wake.next_due_at is not None and wake.next_due_at > NOW
    assert wake.error_streak == 1
    assert not wake.exhausted

    exhausted = next_wake(
        _continuous(),
        RunOutcome.RETRYABLE_ERROR,
        now=NOW,
        error_streak=MAX_ERROR_STREAK - 1,
        rng=RNG,
    )
    assert exhausted.exhausted
    assert exhausted.next_due_at is None


def test_an_empty_iteration_does_not_forgive_a_failing_vendor() -> None:
    """Alternating error / idle would otherwise never reach the error cap.

    A bot whose vendor call always fails but whose precondition usually says
    "nothing to do" would pay for a failing turn forever: each error adds one,
    each empty wake reset it to zero, and the cap was never reached.
    """
    wake = next_wake(
        _continuous(), RunOutcome.NO_WORK, now=NOW, idle_streak=0, error_streak=3, rng=RNG
    )
    assert wake.error_streak == 3


def test_work_done_clears_an_error_streak() -> None:
    """One success means the loop is not stuck, whatever came before it."""
    wake = next_wake(_continuous(), RunOutcome.WORK_DONE, now=NOW, error_streak=4, rng=RNG)
    assert wake.error_streak == 0


# ── rrule ─────────────────────────────────────────────────────────


def test_a_daily_bot_that_was_down_for_a_week_fires_once() -> None:
    """Missed occurrences coalesce. Replaying a backlog is the failure here."""
    week_later = NOW + 7 * 86_400
    following = next_occurrence("FREQ=DAILY;BYHOUR=9", week_later)
    assert following is not None
    assert week_later < following <= week_later + 86_400


def test_an_rrule_wake_is_always_in_the_future() -> None:
    for offset in (0, 1, 3599, 86_399):
        following = next_occurrence("FREQ=HOURLY", NOW + offset)
        assert following is not None and following > NOW + offset


def test_a_finite_rule_that_has_run_out_stops_scheduling() -> None:
    assert next_occurrence("FREQ=DAILY;COUNT=1", NOW) is None


def test_an_unparseable_rule_is_refused_rather_than_silently_never_firing() -> None:
    with pytest.raises(InvalidWakePolicy):
        next_occurrence("EVERY TUESDAY PLEASE", NOW)


# ── activation ────────────────────────────────────────────────────


def test_activation_makes_a_continuous_bot_due_at_once() -> None:
    """So pressing activate visibly does something."""
    assert first_wake(_continuous(), now=NOW).next_due_at == NOW


def test_activation_leaves_event_and_manual_bots_unscheduled() -> None:
    listening = first_wake(WakePolicy(kind=WakeKind.ON_MESSAGE), now=NOW)
    assert listening.next_due_at is None and listening.reason is WakeReason.EVENT

    manual = first_wake(WakePolicy(kind=WakeKind.MANUAL), now=NOW)
    assert manual.next_due_at is None and manual.reason is WakeReason.MANUAL


# ── the policy itself ─────────────────────────────────────────────


def test_a_continuous_policy_without_a_precondition_is_refused() -> None:
    """The one rule in the model with teeth.

    Without a cheap check, every wake costs a vendor turn to learn nothing.
    Ten such bots at an hourly floor is ~240 turns a day for no work.
    """
    with pytest.raises(InvalidWakePolicy, match="precondition"):
        WakePolicy(kind=WakeKind.CONTINUOUS)


def test_a_scheduled_policy_needs_no_precondition() -> None:
    """A bot that fires daily at 09:00 is allowed to just fire."""
    WakePolicy(kind=WakeKind.RRULE, rrule="FREQ=DAILY")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": WakeKind.RRULE},
        {"kind": WakeKind.CONTINUOUS, "precondition": "always", "factor": 0.5},
        {"kind": WakeKind.CONTINUOUS, "precondition": "always", "max_s": 10, "base_s": 60},
        {"kind": WakeKind.CONTINUOUS, "precondition": "always", "jitter": 2.0},
        {"kind": WakeKind.CONTINUOUS, "precondition": "always", "min_interval_s": 0},
    ],
)
def test_an_incoherent_policy_is_refused_at_definition_time(kwargs: dict) -> None:
    with pytest.raises(InvalidWakePolicy):
        WakePolicy(**kwargs)


def test_a_policy_survives_a_round_trip_through_json() -> None:
    """Definitions are stored as JSON, so this is a storage correctness test."""
    for policy in (
        WakePolicy(kind=WakeKind.RRULE, rrule="FREQ=WEEKLY;BYDAY=MO"),
        _continuous(min_interval_s=30, base_s=45, max_s=900, factor=3.0),
        WakePolicy(kind=WakeKind.ON_MESSAGE, sources=("bot:*", "human:dpal")),
        WakePolicy(kind=WakeKind.MANUAL),
    ):
        assert WakePolicy.from_dict(policy.to_dict()) == policy


@pytest.mark.parametrize(
    ("pattern", "author", "expected"),
    [
        (("bot:*",), "bot:abc", True),
        (("bot:*",), "human:dpal", False),
        (("human:dpal",), "human:dpal", True),
        (("human:dpal",), "human:other", False),
        ((), "anyone", True),
        (("*",), "anyone", True),
    ],
)
def test_on_message_sources_are_matched_by_prefix(
    pattern: tuple[str, ...], author: str, expected: bool
) -> None:
    policy = WakePolicy(kind=WakeKind.ON_MESSAGE, sources=pattern)
    assert policy.accepts_sender(author) is expected
