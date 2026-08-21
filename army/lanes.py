"""Per-vendor admission control for subscription quota.

Omnigent's budget policies are denominated in dollars — ``cost_budget`` and
``user_daily_cost_budget`` both take ``max_cost_usd``. On subscription auth
there is no dollar signal at all, so none of that machinery fires and nothing
slows the loop down before the vendor does.

What is actually scarce is different: every sub-agent on one harness shares
that harness's single login, so concurrency is bounded *per vendor*, not
globally. One worker pool with a global cap is the wrong shape — it will happily
put eight sessions on Claude and none on Codex.

So: one lane per harness, each with its own concurrency cap and its own
cooldown. A vendor that reports a rate limit puts only its own lane to sleep.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Lane:
    """One vendor's share of the work.

    :param harness: Omnigent harness id, e.g. ``"claude-native"``.
    :param max_concurrent: How many sessions may run on this vendor at once.
    :param in_flight: How many are running now.
    :param cooldown_until: Unix epoch seconds before which this lane refuses
        new work, set when the vendor reports a limit.
    """

    harness: str
    max_concurrent: int
    in_flight: int = 0
    cooldown_until: int = 0

    def available(self, *, now: int) -> int:
        """
        How many more sessions this lane will take.

        :param now: Unix epoch seconds.
        :returns: Free slots, or ``0`` while cooling down.
        """
        if now < self.cooldown_until:
            return 0
        return max(0, self.max_concurrent - self.in_flight)


class Lanes:
    """Admission control across every configured vendor.

    :param lanes: One :class:`Lane` per harness the loop dispatches to.
    :param default_cooldown_seconds: How long a rate-limited lane sleeps.
    """

    def __init__(self, lanes: list[Lane], *, default_cooldown_seconds: int = 300) -> None:
        self._lanes: dict[str, Lane] = {lane.harness: lane for lane in lanes}
        self.default_cooldown_seconds = default_cooldown_seconds

    @staticmethod
    def from_config(config: dict[str, int], **kwargs: int) -> Lanes:
        """
        Build lanes from a ``{harness: max_concurrent}`` mapping.

        :param config: Per-harness concurrency caps.
        :param kwargs: Passed through to the constructor.
        :returns: The configured lanes.
        """
        return Lanes([Lane(harness, cap) for harness, cap in config.items()], **kwargs)

    def has_capacity(self, *, now: int | None = None) -> bool:
        """
        Whether any lane would accept work right now.

        :param now: Unix epoch seconds; defaults to the clock.
        :returns: ``True`` when at least one lane has a free slot.
        """
        stamp = int(time.time()) if now is None else now
        return any(lane.available(now=stamp) > 0 for lane in self._lanes.values())

    def pick(self, *, now: int | None = None) -> str | None:
        """
        Choose the emptiest lane that will take work.

        Emptiest rather than round-robin, so a loop that fans out spreads
        across vendors instead of stacking on whichever one it named first —
        which is also what makes cross-vendor review cheap.

        :param now: Unix epoch seconds; defaults to the clock.
        :returns: A harness id, or ``None`` when everything is full or cooling.
        """
        stamp = int(time.time()) if now is None else now
        candidates = [
            (lane.available(now=stamp), lane.harness)
            for lane in self._lanes.values()
            if lane.available(now=stamp) > 0
        ]
        if not candidates:
            return None
        return max(candidates)[1]

    def acquire(self, count: int = 1, harness: str | None = None) -> None:
        """
        Record that sessions have started.

        :param count: How many.
        :param harness: Which lane, or ``None`` to spread across lanes with
            room (used when the caller let :meth:`pick` choose).
        """
        if harness is not None:
            lane = self._lanes.get(harness)
            if lane is not None:
                lane.in_flight += count
            return
        for _ in range(count):
            chosen = self.pick()
            if chosen is None:
                return
            self._lanes[chosen].in_flight += 1

    def release(self, count: int = 1, harness: str | None = None) -> None:
        """
        Record that sessions have finished.

        :param count: How many.
        :param harness: Which lane, or ``None`` to drain the busiest lanes
            first, mirroring how :meth:`acquire` filled the emptiest.
        """
        if harness is not None:
            lane = self._lanes.get(harness)
            if lane is not None:
                lane.in_flight = max(0, lane.in_flight - count)
            return
        for _ in range(count):
            busiest = max(self._lanes.values(), key=lambda lane: lane.in_flight, default=None)
            if busiest is None or busiest.in_flight == 0:
                return
            busiest.in_flight -= 1

    def rate_limited(
        self,
        harness: str,
        *,
        seconds: int | None = None,
        now: int | None = None,
    ) -> None:
        """
        Put one lane to sleep after a vendor reported a limit.

        Only that vendor's lane. A 429 from one subscription says nothing about
        the others, and stopping the whole loop for it would waste the capacity
        that is still there.

        :param harness: The lane that hit the limit.
        :param seconds: How long to sleep, or ``None`` for the default.
        :param now: Unix epoch seconds; defaults to the clock.
        """
        lane = self._lanes.get(harness)
        if lane is None:
            return
        stamp = int(time.time()) if now is None else now
        lane.cooldown_until = stamp + (seconds or self.default_cooldown_seconds)

    def depth(self, *, now: int | None = None) -> dict[str, dict[str, int]]:
        """
        Report each lane's occupancy, for the master to plan around.

        :param now: Unix epoch seconds; defaults to the clock.
        :returns: Per-harness ``in_flight`` / ``max`` / ``available``.
        """
        stamp = int(time.time()) if now is None else now
        return {
            lane.harness: {
                "in_flight": lane.in_flight,
                "max": lane.max_concurrent,
                "available": lane.available(now=stamp),
                "cooldown_until": lane.cooldown_until,
            }
            for lane in self._lanes.values()
        }


#: Sensible starting point for the six target harnesses. Two apiece keeps a
#: single subscription well inside its limits while still giving cross-vendor
#: review something to work with; raise a lane once you have seen it saturate.
DEFAULT_LANES: dict[str, int] = {
    "claude-native": 2,
    "codex-native": 2,
    "antigravity-native": 2,
    "grok": 2,
    "kimi-native": 2,
    "pi-native": 2,
}
