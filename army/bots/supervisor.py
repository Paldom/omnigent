"""The tick that turns due bots into runs, and finished runs into next wakes.

One pass does two different jobs at two different cadences, and keeping them
distinct is most of the design:

- **Advancing runs** is the loop ``army`` already had. A bot's iteration is an
  ordinary :class:`~army.state.Run` and walks the same states.
- **Waking bots** is a query over ``bots``, not over ``runs``. It asks which
  active bots are due, checks a model-free precondition, and opens exactly one
  run for the first that has work.

The join between them is :meth:`BotSupervisor._transition`, which is where an
iteration ending and the bot's next wake are written *together*. That pairing
is not an optimisation. A crash between those two writes either repeats the
iteration or loses the bot forever, and there is no second "bump the bot" path
to remember, because there is no second path at all.
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import Any

from army.bots.model import Bot, BotStatus, RunOutcome, WakeKind
from army.bots.precondition import PreconditionRegistry, UnknownPrecondition
from army.bots.registry import WorkloadRefused, WorkloadRegistry
from army.bots.schedule import Wake, next_wake
from army.bots.store import BotStore
from army.lanes import Lanes
from army.omni import OmniClient
from army.state import Run, RunState
from army.store import ConcurrentTransition, Store
from army.supervisor import Supervisor, TickReport
from army.workload import Workload

_logger = logging.getLogger(__name__)

#: Run states that settle an iteration and so produce an outcome. ``READY`` is
#: here because the requeue after a vendor refusal is how a rate limit reaches
#: the scheduler; ``WAITING_HUMAN`` because parking on a person is the
#: ``BLOCKED`` outcome, and a bot with no next wake must be recorded as such
#: rather than left looking scheduled.
_SETTLING = frozenset(
    {
        RunState.READY,
        RunState.WAITING_HUMAN,
        RunState.CONTINUE,
        RunState.PAUSED,
        RunState.COMPLETED,
        RunState.FAILED,
    }
)


class FleetWorkload:
    """A stand-in for the single workload a fleet does not have.

    :class:`~army.supervisor.Supervisor` is built around one workload because
    that is the honest shape for one job of work. A fleet resolves a workload
    per run instead, so every method here would be a lie — and says so rather
    than guessing. Only :attr:`name` is real, and only so log lines read right.
    """

    name = "bots"

    def _refuse(self, method: str) -> Any:
        raise RuntimeError(
            f"a bot fleet has no single workload; {method}() should have gone "
            "through the run's own workload"
        )

    def acquire(self) -> dict[str, Any] | None:
        return self._refuse("acquire")

    def dispatch(self, run: Run, omni: OmniClient) -> list[str]:  # noqa: ARG002
        return self._refuse("dispatch")  # type: ignore[no-any-return]

    def collect(self, run: Run, omni: OmniClient) -> tuple[bool, dict[str, Any]]:  # noqa: ARG002
        return self._refuse("collect")  # type: ignore[no-any-return]

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:  # noqa: ARG002
        return self._refuse("evaluate")  # type: ignore[no-any-return]

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:  # noqa: ARG002
        return self._refuse("apply")  # type: ignore[no-any-return]


class BotSupervisor(Supervisor):
    """Drives a fleet of bots, each with its own workload and its own schedule.

    :param store: Where run state lives.
    :param omni: Client for the Omnigent server.
    :param bots: Where bot state lives, in the same database.
    :param workloads: Resolves the workload a bot names.
    :param preconditions: Model-free checks a bot may name; defaults to the
        built-ins.
    :param lanes: Per-vendor admission control, or ``None``.
    :param max_concurrent_runs: Iterations in flight across the whole fleet.
    :param scan_limit: Most due bots examined in one tick, so a large roster
        cannot turn one tick into unbounded work.
    """

    def __init__(
        self,
        store: Store,
        omni: OmniClient,
        bots: BotStore,
        workloads: WorkloadRegistry,
        *,
        preconditions: PreconditionRegistry | None = None,
        lanes: Lanes | None = None,
        max_concurrent_runs: int = 3,
        scan_limit: int = 50,
    ) -> None:
        super().__init__(
            store,
            omni,
            FleetWorkload(),
            lanes=lanes,
            max_concurrent_runs=max_concurrent_runs,
        )
        self.bots = bots
        self.workloads = workloads
        self.preconditions = preconditions or PreconditionRegistry()
        self.scan_limit = scan_limit

    # ── the two seams ─────────────────────────────────────────────

    def _active_runs(self) -> list[Run]:
        """Every in-flight bot iteration, whatever workload it belongs to."""
        return [run for run in self.store.active_runs() if run.bot_id is not None]

    def _workload_for(self, run: Run) -> Workload:
        """
        The workload the run's bot names.

        Resolved from the bot rather than the run so an operator who fixes a
        mistyped workload path does not have to rebuild the run — and cached by
        the registry, so this is a dict lookup after the first call.

        :param run: The run being advanced.
        :returns: Its workload.
        :raises WorkloadRefused: If the bot is gone or names something this
            deployment will not load.
        """
        bot = self.bots.get(run.bot_id or "")
        if bot is None:
            raise WorkloadRefused(
                f"run {run.id[:12]} names bot {run.bot_id}, which no longer exists"
            )
        return self.workloads.for_bot(bot)

    # ── waking ────────────────────────────────────────────────────

    def _open_run(self, *, now: int) -> Run | None:
        """
        Find a bot with work and open one iteration for it.

        Every due bot is examined, but at most one run is opened per tick. The
        bots that turn out to have nothing to do are not skipped — they are
        *backed off*, right here, without a body ever being spawned. That is
        the whole economic argument for the precondition: the fleet's idle cost
        is one SQL query per bot per wake, not one vendor turn.

        :param now: Epoch seconds.
        :returns: The run opened, or ``None`` when nothing had work.
        """
        blocked = self.bots.blocked_vendors(now=now)
        opened: Run | None = None
        for bot in self.bots.due(now=now, limit=self.scan_limit):
            if self._retire_if_expired(bot, now=now):
                continue
            if bot.harness is not None and bot.harness in blocked:
                # The vendor gate holds it, not a private backoff. Leaving
                # next_due_at alone means it goes the moment the lane reopens
                # rather than serving a second sentence of its own.
                continue
            if opened is not None:
                # One run per tick, so a fleet that all comes due at once does
                # not open ten sessions in one pass. The rest stay due.
                continue
            if not self._has_work(bot, now=now):
                continue
            opened = self._start(bot, now=now)
        return opened

    def _has_work(self, bot: Bot, *, now: int) -> bool:
        """
        Evaluate the bot's precondition, and back it off if the answer is no.

        :param bot: A due bot.
        :param now: Epoch seconds.
        :returns: Whether to spawn a body.
        """
        name = bot.wake.precondition
        if name is None:
            # Only a continuous bot is required to have one. A scheduled bot
            # that fires daily at 09:00 is allowed to just fire.
            return True
        config = dict(bot.workload_config.get("precondition") or {})
        try:
            if self.preconditions.evaluate(name, config):
                return True
        except UnknownPrecondition:
            # A named check nobody registered is an operator error, not a quiet
            # "no work" — pause the bot so it is visible instead of looking
            # permanently idle.
            _logger.error("bot %s names an unknown precondition %r; pausing it", bot.slug, name)
            self._pause(bot, now=now)
            return False
        self._back_off(bot, now=now)
        return False

    def _back_off(self, bot: Bot, *, now: int) -> None:
        """
        Record an empty wake that cost no vendor turn.

        The bot did not run, so there is no run to carry the outcome — but the
        scheduler still has to learn that the mission was empty, or a
        continuous bot with a cheap check would poll at its floor forever.

        :param bot: The bot that had nothing to do.
        :param now: Epoch seconds.
        """
        wake = next_wake(
            bot.wake,
            RunOutcome.NO_WORK,
            now=now,
            idle_streak=bot.idle_streak,
            error_streak=bot.error_streak,
        )
        try:
            with self.bots.atomic() as conn:
                self.bots.record_wake(bot, wake, RunOutcome.NO_WORK, now=now, conn=conn)
                if wake.exhausted:
                    _logger.warning(
                        "bot %s found nothing to do %d times running; pausing it",
                        bot.slug,
                        wake.idle_streak,
                    )
                    self.bots.set_status(bot, BotStatus.PAUSED, now=now, conn=conn)
        except ConcurrentTransition:
            # Another writer moved the bot. Its decision is as good as ours.
            _logger.debug("bot %s moved while backing it off", bot.slug)

    def _start(self, bot: Bot, *, now: int) -> Run | None:
        """
        Open one iteration for a bot, pinned to its current definition.

        The order here is load-bearing. ``acquire()`` is not a read — the demo
        workload marks its queue line ``taken:``, and a real one may claim a
        row — so an item taken for a run that is then refused is an item
        nobody will do. The live-run check therefore happens *before* the
        acquire, and a refusal afterwards is logged as the bug it is rather
        than swallowed as ordinary contention.

        :param bot: The bot to run.
        :param now: Epoch seconds.
        :returns: The new run, or ``None`` when there was nothing to do.
        """
        try:
            workload = self.workloads.for_bot(bot)
        except WorkloadRefused:
            _logger.exception("bot %s cannot be dispatched; pausing it", bot.slug)
            self._pause(bot, now=now)
            return None

        if self._has_live_run(bot):
            # Re-read rather than trust the scan: the query that found this bot
            # due ran before the loop, and taking a work item for a bot that is
            # already busy would strand it.
            _logger.debug("bot %s picked up a run since the scan; leaving it", bot.slug)
            return None

        item = self._acquire_for(workload, bot)
        if item is None:
            # The precondition said yes and the queue says no. Not worth
            # failing over — a coarse check, or a sibling got there first —
            # but it is an empty iteration, so it backs the bot off.
            self._back_off(bot, now=now)
            return None

        run = Run.new(
            workload.name,
            item,
            now=now,
            bot_id=bot.id,
            revision_id=bot.current_revision_id,
        )
        try:
            return self.store.create_run(run)
        except ConcurrentTransition:
            # Reaching here means the live-run check above was wrong, which is
            # a bug in this method and not a signal the control plane should
            # act on. Say so loudly, and say what it cost: the work item is
            # already claimed and no run will do it.
            _logger.error(
                "bot %s: the one-live-run index refused a run the live check allowed. "
                "Work item %r was taken from %s and is now stranded — requeue it by hand. "
                "This is a supervisor bug, not contention.",
                bot.slug,
                item,
                workload.name,
            )
            return None

    def _has_live_run(self, bot: Bot) -> bool:
        """
        Whether this bot already has a non-terminal run.

        :param bot: The bot.
        :returns: Whether one is in flight.
        """
        return bot.id in self.bots.live_run_states()

    @staticmethod
    def _acquire_for(workload: Workload, bot: Bot) -> dict[str, Any] | None:
        """
        Take a work item, scoped to this bot when the workload can do that.

        Two bots sharing one workload share its queue, and a plain ``acquire()``
        gives them no way to divide it — whoever ticks first takes the item.
        A workload that cares implements ``acquire_for(bot)``; one that does
        not keeps the old behaviour, which is correct for the common case of
        one bot per workload.

        :param workload: The bot's workload.
        :param bot: The bot acquiring.
        :returns: The work item, or ``None`` when there is none.
        """
        scoped = getattr(workload, "acquire_for", None)
        if callable(scoped):
            return scoped(bot)  # type: ignore[no-any-return]
        return workload.acquire()

    def _retire_if_expired(self, bot: Bot, *, now: int) -> bool:
        """
        Retire a spawned bot whose TTL has passed.

        :param bot: A due bot.
        :param now: Epoch seconds.
        :returns: Whether it was retired.
        """
        if bot.expires_at is None or bot.expires_at > now:
            return False
        _logger.info("bot %s reached its expiry; retiring it", bot.slug)
        with suppress(ConcurrentTransition):
            self.bots.set_status(bot, BotStatus.RETIRED, now=now)
        return True

    def _pause(self, bot: Bot, *, now: int) -> None:
        """Stop scheduling a bot, tolerating a race with another writer."""
        try:
            self.bots.set_status(bot, BotStatus.PAUSED, now=now)
        except ConcurrentTransition:
            _logger.debug("bot %s moved while pausing it", bot.slug)

    # ── succession ────────────────────────────────────────────────

    def _transition(self, run: Run, target: RunState, **kwargs: Any) -> Run:
        """
        Move a run, and — when the move settles the iteration — its bot with it.

        One transaction covers three writes that must not be separated: the
        run's terminal transition, the command it consumed, and the bot's next
        wake. Written apart, a crash in the gap leaves a bot whose work is done
        and whose ``next_due_at`` is still ``NULL``, and ``NULL <= now`` is
        false in SQLite — so the scan skips it and the bot sleeps forever with
        its work finished. Every reviewer of the design caught that one.

        :param run: The run as read.
        :param target: Where to move it.
        :param kwargs: Passed through to the store.
        :returns: The run after its move.
        """
        if run.bot_id is None or target not in _SETTLING:
            return super()._transition(run, target, **kwargs)

        now = kwargs.get("now") or int(time.time())
        bot = self.bots.get(run.bot_id)
        if bot is None:
            # An orphaned run. Let it finish; there is nothing to schedule.
            return super()._transition(run, target, **kwargs)

        outcome = classify(run, target)
        wake = next_wake(
            bot.wake,
            outcome,
            now=now,
            idle_streak=bot.idle_streak,
            error_streak=bot.error_streak,
        )
        if outcome is RunOutcome.RATE_LIMITED:
            self._cool_vendor(run, now=now)

        with self.bots.atomic() as conn:
            moved = self.store.transition(run, target, outcome=outcome.value, conn=conn, **kwargs)
            self.bots.record_wake(bot, wake, outcome, now=now, conn=conn)
            if wake.exhausted:
                _logger.warning(
                    "bot %s exhausted a streak (idle=%d error=%d); pausing it",
                    bot.slug,
                    wake.idle_streak,
                    wake.error_streak,
                )
                self.bots.set_status(bot, BotStatus.PAUSED, now=now, conn=conn)
        return moved

    def _cool_vendor(self, run: Run, *, now: int) -> None:
        """
        Record a vendor limit where a restart cannot forget it.

        ``Lane.cooldown_until`` lives in RAM and is rebuilt empty on every
        start, so without this a reboot makes every bot on a limited vendor
        instantly due again — the quota spin the outcome model exists to stop.

        :param run: The run that hit the limit.
        :param now: Epoch seconds.
        """
        harness = str(run.artifacts.get("rate_limited_harness") or "")
        if not harness:
            return
        seconds = self.lanes.default_cooldown_seconds if self.lanes is not None else 300
        reason = str(run.artifacts.get("rate_limited_phrase") or "vendor reported a limit")
        self.bots.block_vendor(harness, now + seconds, reason)

    # ── operator view ─────────────────────────────────────────────

    def fleet_tick(self, *, now: int | None = None) -> TickReport:
        """
        One pass, plus the alarm for bots nothing will ever wake.

        :param now: Epoch seconds; defaults to the clock.
        :returns: What the pass did.
        """
        stamp = int(time.time()) if now is None else now
        report = self.tick(now=stamp)
        for bot in self.bots.stalled(now=stamp):
            # A lost succession is otherwise invisible: the bot is active, has
            # no run, and nothing is scheduled to notice.
            _logger.error(
                "bot %s is active with no run and no next wake (last outcome %s) — "
                "its succession was lost; `army bots wake %s` restarts it",
                bot.slug,
                bot.last_outcome.value if bot.last_outcome else "none",
                bot.slug,
            )
        return report


def classify(run: Run, target: RunState) -> RunOutcome:
    """
    Say what an iteration achieved, in the terms the scheduler understands.

    The workload gets the first word: it is the only thing that knows whether
    ``CONTINUE`` meant "merged a change" or "looked and found nothing", and
    those two want opposite wake times. It says so by putting an ``outcome`` in
    the run's artifacts. Everything else is read from the move itself.

    :param run: The run being settled.
    :param target: The state it is moving to.
    :returns: The outcome.
    """
    declared = run.artifacts.get("outcome")
    if isinstance(declared, str):
        try:
            return RunOutcome(declared)
        except ValueError:
            _logger.warning(
                "run %s declared an unknown outcome %r; classifying it from the move instead",
                run.id[:12],
                declared,
            )

    if target is RunState.WAITING_HUMAN:
        # Parked on a person. No next wake at all — the verdict's own
        # transaction is what schedules the bot again.
        return RunOutcome.BLOCKED
    if target is RunState.READY:
        # The only way back to READY from a working state is the requeue after
        # a vendor refused on quota.
        return RunOutcome.RATE_LIMITED
    if target is RunState.FAILED:
        return RunOutcome.RETRYABLE_ERROR
    if target is RunState.PAUSED:
        # A human stopped this branch. Nothing about the mission was learned,
        # so neither streak should move — and BLOCKED is the outcome that
        # leaves both alone.
        return RunOutcome.BLOCKED
    return RunOutcome.WORK_DONE


def wake_now(
    bots: BotStore,
    bot: Bot,
    *,
    now: int,
    reason: str = "human",
) -> Bot:
    """
    Make a bot due immediately.

    The insert-driven half of the wake model. A bot with ``next_due_at IS
    NULL`` — blocked on a person, or waiting on a message — is woken by
    something happening, not by time passing, and this is that something.

    :param bots: The bot store.
    :param bot: The bot to wake.
    :param now: Epoch seconds.
    :param reason: What woke it, for the derived status.
    :returns: The bot, now due.
    """
    from army.bots.model import WakeReason

    resolved = WakeReason(reason) if reason in {r.value for r in WakeReason} else WakeReason.HUMAN
    wake = Wake(now, resolved, bot.idle_streak, bot.error_streak)
    return bots.record_wake(bot, wake, None, now=now)


def is_event_driven(bot: Bot) -> bool:
    """Whether an insert rather than a clock is what wakes this bot."""
    return bot.wake.kind is WakeKind.ON_MESSAGE
