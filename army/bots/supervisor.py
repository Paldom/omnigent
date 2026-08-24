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

from army.bots.approvals import ITERATION_GATE, ApprovalRequest, ApprovalStore
from army.bots.budget import BudgetExhausted, BudgetStore
from army.bots.isolation import sandbox_for
from army.bots.messages import MessageKind, MessageStore
from army.bots.model import Bot, BotStatus, IllegalBotMove, RunOutcome, WakeKind
from army.bots.precondition import PreconditionRegistry, UnknownPrecondition
from army.bots.registry import WorkloadRefused, WorkloadRegistry
from army.bots.schedule import Wake, next_wake
from army.bots.store import BotStore
from army.lanes import Lanes
from army.omni import OmniClient, OmniError, barrier_marker
from army.state import Command, Run, RunState
from army.store import ConcurrentTransition, Store
from army.supervisor import Supervisor, TickReport, _asking_session
from army.workload import Workload

_logger = logging.getLogger(__name__)

#: Run states that settle an iteration and so produce an outcome.
#:
#: ``READY`` is deliberately absent. The requeue after a vendor refusal goes
#: back to ``READY``, which is still a *live* run — the partial unique index
#: and the due scan both treat only continue/completed/failed as dead. Settling
#: there would record an outcome and a next wake for an iteration that has not
#: ended, and then settle a second time when the run really does finish. The
#: vendor limit is recorded by :meth:`BotSupervisor._requeue_rate_limited`
#: instead, which is where the vendor is actually known.
#:
#: ``WAITING_HUMAN`` is present because parking on a person genuinely ends the
#: iteration's use of a body, and a bot with no next wake must be recorded as
#: such rather than left looking scheduled.
_SETTLING = frozenset(
    {
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
        messages: MessageStore | None = None,
        approvals: ApprovalStore | None = None,
        budgets: BudgetStore | None = None,
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
        # The channel and the approval ledger are optional so the scheduler can
        # be tested without them, but a deployment always has both: an ask that
        # exists only in a run's artifacts is not addressable, and a verdict
        # that binds nothing is not a verdict.
        self.messages = messages
        self.approvals = approvals
        # No ledger means unlimited, which is the right default for a
        # top-level bot nobody has thought about yet — and the wrong one for
        # anything that can spawn, which is why `carve` refuses an
        # unbudgeted parent.
        self.budgets = budgets
        #: Bots already reported as stalled, so the alarm fires on the change
        #: rather than on every tick.
        self._alarmed: set[str] = set()

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
        # The pinned revision, not the live row. An operator who edits a bot's
        # workload mid-iteration would otherwise have `dispatch` run against
        # one and `collect` against another — one iteration, two jobs, and
        # evidence that describes neither.
        if run.revision_id is not None:
            revision = self.bots.revision(run.revision_id)
            if revision is not None:
                pinned = str(revision.definition.get("workload") or bot.workload)
                options = revision.definition.get("workload_config") or {}
                try:
                    return self.workloads.resolve(pinned, options)
                except WorkloadRefused as exc:
                    raise WorkloadRefused(
                        f"bot {bot.slug!r} (revision {revision.rev}): {exc}"
                    ) from exc
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
            self._pause(
                bot,
                now=now,
                reason=f"names a precondition nobody registered: {name!r}",
            )
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
                    self.bots.set_status(
                        bot,
                        BotStatus.PAUSED,
                        now=now,
                        reason=f"found nothing to do {wake.idle_streak} times running",
                        conn=conn,
                    )
        except ConcurrentTransition:
            # Another writer moved the bot — usually a human pausing or waking
            # it, whose decision beats ours. Logged at WARNING rather than
            # DEBUG because the failure mode if this is *not* a human is a bot
            # that never backs off and polls at full rate, which is a bill.
            _logger.warning(
                "bot %s could not be backed off (intended next wake %s); it stays due "
                "and will be re-examined next tick",
                bot.slug,
                wake.next_due_at,
            )

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
            self._pause(bot, now=now, reason="its workload could not be loaded")
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

        # Anything the operator said while this bot was idle rides into the
        # iteration's payload, so the body reads it before the work item. A
        # message that only reaches the channel is a message read a week later
        # as history.
        item = {**item, "said": self._take_messages(bot, now=now)}
        run = Run.new(
            workload.name,
            item,
            now=now,
            bot_id=bot.id,
            revision_id=bot.current_revision_id,
        )
        # What this iteration is *permitted* to touch, recorded on the run
        # itself. `profile_for` existed and nothing called it, which made the
        # isolation story a design rather than a control — and an uncalled
        # security helper reads exactly like an enforced one to anybody
        # skimming.
        #
        # Recording is not enforcing, and the gap is named in the artifact
        # rather than left to be discovered: Omnigent's own file tools respect
        # an environment root, the vendor CLIs' native tools do not, so on a
        # native harness this profile is the intent and not the boundary. It
        # is still worth writing down — an incident review wants to know what
        # the run was allowed to do, and "nobody recorded it" is the worst
        # possible answer.
        run.artifacts["sandbox"] = sandbox_for(bot)
        try:
            with self.bots.atomic() as conn:
                self.store.create_run(run, conn=conn)
                if self.budgets is not None:
                    # In the same transaction that materialises the run, so a
                    # crash cannot leave an iteration nobody paid for — which
                    # over a long enough night is how a budget stops meaning
                    # anything.
                    self.budgets.charge(bot.id, run_id=run.id, now=now, conn=conn)
            return run
        except BudgetExhausted as exc:
            # The only backstop against a poison mission: a bot that reports
            # work done every time has no idle streak to back it off. Pause and
            # say so — a silent stall looks exactly like a bot that is working.
            # Rewritten with the slug: `charge` only knows the id, and an
            # operator reading their own channel should not have to look one up.
            reason = f"budget exhausted — refill it with `army bots budget {bot.slug} --grant N`"
            _logger.warning("bot %s: %s", bot.slug, exc)
            self._pause(bot, now=now, reason=reason)
            self._say(bot, f"Paused: {reason}", now=now)
            return None
        except ConcurrentTransition:
            # The live-run check above raced something — another tick, or the
            # CLI. The index did its job; the problem is the work item, which
            # `acquire` has already claimed. Hand it back if the workload knows
            # how, and say plainly what was lost if it does not.
            if self._release(workload, item):
                _logger.info(
                    "bot %s: the run was refused after acquiring %s; the item was returned",
                    bot.slug,
                    _describe(item),
                )
            else:
                _logger.error(
                    "bot %s: the one-live-run index refused a run after %s was taken from %s, "
                    "and that workload has no release() — the item is stranded and must be "
                    "requeued by hand.",
                    bot.slug,
                    _describe(item),
                    workload.name,
                )
            return None

    def _say(self, bot: Bot, body: str, *, now: int) -> None:
        """
        Put a line in the bot's channel, when there is one.

        For the things an operator only finds out about by reading logs
        otherwise: a pause, a refill, an expiry.

        :param bot: Whose channel.
        :param body: What to say.
        :param now: Epoch seconds.
        """
        if self.messages is None:
            return
        with suppress(Exception):
            self.messages.post(bot.id, "system", MessageKind.EVENT, body, now=now)

    @staticmethod
    def _release(workload: Workload, item: dict[str, Any]) -> bool:
        """
        Give a claimed work item back, when the workload knows how.

        ``acquire`` has a side effect — the demo workload marks its queue line
        ``taken:`` — so an item taken for a run that is then refused is an item
        nobody will do. A workload that cares implements ``release(item)``.

        :param workload: The workload the item came from.
        :param item: The item to hand back.
        :returns: Whether it was returned.
        """
        hook = getattr(workload, "release", None)
        if not callable(hook):
            return False
        try:
            hook(item)
        except Exception:
            _logger.exception("workload %s could not release an item", workload.name)
            return False
        return True

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
        try:
            self.bots.set_status(bot, BotStatus.RETIRED, now=now)
        except (ConcurrentTransition, IllegalBotMove) as exc:
            # Claiming it was retired when the write lost would skip the bot
            # this tick *and* leave it ACTIVE — the worst of both. Say it did
            # not happen; the next tick tries again from a fresh read.
            _logger.warning(
                "bot %s reached its expiry but could not be retired: %s", bot.slug, exc
            )
            return False
        _logger.info("bot %s reached its expiry and was retired", bot.slug)
        self._say(bot, "Retired: its time-to-live expired.", now=now)
        return True

    def _pause(self, bot: Bot, *, now: int, reason: str) -> None:
        """
        Stop scheduling a bot, recording why and tolerating a race.

        :param bot: The bot.
        :param now: Epoch seconds.
        :param reason: What the system objected to. Without it a bot paused by
            a transient fault is indistinguishable from one a person stopped,
            so nobody restarts it when the fault is fixed.
        """
        try:
            self.bots.set_status(bot, BotStatus.PAUSED, now=now, reason=reason)
        except (ConcurrentTransition, IllegalBotMove) as exc:
            _logger.warning("bot %s could not be paused (%s); retrying next tick", bot.slug, exc)

    # ── asking ────────────────────────────────────────────────────

    def _ask(self, run: Run, *, now: int) -> Run | None:
        """
        Put the iteration's question where it will outlive the process asking.

        The base class posts into an Omnigent session and parks on the id it
        gets back, which is right when a session is where the work happened —
        but it means an iteration that spawned no session cannot ask at all,
        and the base class fails the run rather than parking it. For a bot that
        is the wrong answer twice over: the question is the point of the
        iteration, and the row is the record, not the transcript.

        So the question is written into the run either way, and the session is
        only where it is *also* shown. A bot whose workload needs no sessions
        still asks, and is still answerable with ``army approve``.

        :param run: The run, in ``EVALUATING``.
        :param now: Epoch seconds.
        :returns: The run, parked on a person.
        """
        question, options, evidence = self._workload_for(run).evaluate(run)
        artifacts = {
            **run.artifacts,
            "question": question,
            "options": options,
            "evidence": evidence,
        }
        approval_id = f"barrier_{run.id}"

        # The ledger row, and the channel message that renders it. Written
        # before the transition so a crash leaves an unanswered question rather
        # than a run parked on a question that was never recorded — and
        # withdrawn below if the transition then loses its race.
        request = self._record_ask(run, question, options, evidence, now=now)
        if request is not None:
            approval_id = request.id
            artifacts["approval_request_id"] = request.id

        session_id = _asking_session(run)
        if session_id is not None:
            try:
                approval_id = self.omni.ask(
                    run.id, session_id, question, options, evidence=evidence
                )
            except OmniError as exc:
                if exc.is_transient:
                    # The row would be right and the notification would not.
                    # Retry next tick rather than parking on a question nobody
                    # was shown.
                    _logger.warning("could not post the question for run %s: %s", run.id, exc)
                    return None
                _logger.warning(
                    "run %s: could not post the question to session %s (%s); "
                    "parking on the durable row instead — answer with `army approve %s`",
                    run.id[:12],
                    session_id,
                    exc,
                    run.id[:12],
                )

        try:
            return self._transition(
                run,
                RunState.WAITING_HUMAN,
                artifacts=artifacts,
                approval_id=approval_id,
                now=now,
            )
        except Exception:
            # The question was recorded and the run did not park, so nobody is
            # waiting on it. Leaving it pending would show the operator a
            # question whose answer could never be applied.
            if request is not None and self.approvals is not None:
                self.approvals.cancel(request, now=now, reason="the run did not park")
            raise

    def _record_ask(
        self,
        run: Run,
        question: str,
        options: list[str],
        evidence: dict[str, Any],
        *,
        now: int,
    ) -> ApprovalRequest | None:
        """
        Write the approval row and the channel message that shows it.

        Both or neither: an ask in the ledger that nobody can see is a bot that
        looks stuck for no reason, and a message with no ledger row behind it is
        a question a verdict cannot be bound to.

        The verb is :data:`~army.bots.approvals.ITERATION_GATE` — deliberately
        not the name of anything a bot can do. "May this iteration continue" and
        "may you spend money" must never be the same question with a different
        label, and the owner-only verbs never come through here at all.

        :param run: The run about to park.
        :param question: What a person reads.
        :param options: The choices.
        :param evidence: What they should see first.
        :param now: Epoch seconds.
        :returns: The request, or ``None`` when no ledger is configured.
        """
        if self.approvals is None or run.bot_id is None:
            return None
        request = self.approvals.request(
            bot_id=run.bot_id,
            run_id=run.id,
            # The version the run will be *parked at*, not the one it is
            # leaving. The move into WAITING_HUMAN is a compare-and-swap on the
            # version read here, so it lands at exactly one more — and while the
            # run waits, nothing moves it further. Binding the pre-move version
            # instead makes every verdict look stale the moment it is asked.
            run_version=run.version + 1,
            verb=ITERATION_GATE,
            # The hash covers the iteration's evidence, so an answer given to
            # one set of findings cannot be applied to a different set.
            parameters={"run": run.id, "evidence": evidence},
            question=question,
            options=list(options),
            evidence=evidence,
            thread_id=run.id,
            now=now,
        )
        if self.messages is not None:
            self.messages.post(
                run.bot_id,
                "system",
                MessageKind.ASK,
                question,
                now=now,
                payload={
                    "approval_id": request.id,
                    "options": list(options),
                    "evidence": evidence,
                },
                thread_id=run.id,
                run_id=run.id,
                # Owed to a person, so it shows up in "what needs you" until
                # somebody actually answers it.
                deliver_to=["human:owner"],
            )
        return request

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

        # `or` would read a deliberate now=0 as "unset" and silently use the
        # wall clock, which makes a test that pins the epoch pass for the wrong
        # reason and a replay drift.
        supplied = kwargs.get("now")
        now = int(time.time()) if supplied is None else supplied
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
        # The single most important write in the system, and it used to emit
        # nothing. At 3am the symptom is a stalled alarm with no run id, no
        # outcome, and no way to tell which transition wrote the NULL.
        _logger.info(
            "bot %s: run %s -> %s (%s); next wake %s%s",
            bot.slug,
            run.id[:12],
            target.value,
            outcome.value,
            wake.next_due_at if wake.next_due_at is not None else "none",
            " [streak exhausted]" if wake.exhausted else "",
        )

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
                self.bots.set_status(
                    bot,
                    BotStatus.PAUSED,
                    now=now,
                    reason=(
                        f"gave up after {wake.error_streak} failures"
                        if wake.error_streak
                        else f"found nothing to do {wake.idle_streak} times running"
                    ),
                    conn=conn,
                )
        return moved

    def _requeue_rate_limited(
        self, run: Run, limited: tuple[str, str, str], *, now: int
    ) -> Run | None:
        """
        Cool the vendor durably, then requeue as the base class does.

        This is where the vendor is actually known — the base class was handed
        ``(session, harness, phrase)`` by the check that found the refusal.
        Reading it back out of the run's artifacts later meant a workload that
        did not record them produced no gate at all, silently, and after a
        restart every bot on that vendor fired at once.

        ``Lane.cooldown_until`` lives in RAM and is rebuilt empty on every
        start, which is the whole reason ``provider_gates`` exists.

        :param run: The run whose session was refused.
        :param limited: ``(session_id, harness, phrase)``.
        :param now: Epoch seconds.
        :returns: The requeued run.
        """
        _session_id, harness, phrase = limited
        seconds = self.lanes.default_cooldown_seconds if self.lanes is not None else 300
        self.bots.block_vendor(harness, now + seconds, phrase)
        _logger.warning(
            "%s reported %r; the lane is closed until %d and every bot on it waits",
            harness,
            phrase,
            now + seconds,
        )
        return super()._requeue_rate_limited(run, limited, now=now)

    def _dispatch(self, run: Run, *, now: int) -> Run | None:
        """
        Hold a run back while its vendor is cooling, durably.

        The base class consults the in-memory lanes, which are empty after a
        restart. A run already sitting in ``READY`` because that vendor refused
        it is *live*, so the wake scan never sees it and never applies the
        durable gate — and the first tick after a reboot sends it straight back
        to the vendor that just said stop.

        :param run: The run in ``READY``.
        :param now: Epoch seconds.
        :returns: The run after dispatching, or ``None`` while it waits.
        """
        bot = self.bots.get(run.bot_id or "")
        if bot is not None and bot.harness is not None:
            blocked = self.bots.blocked_vendors(now=now)
            until = blocked.get(bot.harness)
            if until is not None:
                _logger.debug(
                    "holding %s: the %s lane is closed for another %ds",
                    bot.slug,
                    bot.harness,
                    until - now,
                )
                return None
        return super()._dispatch(run, now=now)

    # ── the authorisation path ────────────────────────────────────

    def _command_from_chat_reply(self, run: Run, *, now: int) -> Command | None:
        """
        Refuse to turn typed prose into an authorisation.

        The base class reads the session transcript and converts a reply that
        names one offered option into an ``APPROVE`` command. That is a genuine
        convenience — it is the phone answer path — and it is also a second
        authorisation route that consults none of the bindings an approval is
        supposed to carry. A binding one answer path ignores is not a binding,
        so for a bot there is exactly one route to a verdict and it goes
        through :class:`~army.bots.approvals.ApprovalStore`.

        The reply is not lost. It lands in the bot's channel as an ordinary
        message, where a person can see what was said and answer properly.

        :param run: The run parked on a person.
        :param now: Epoch seconds.
        :returns: Always ``None``.
        """
        if self.approvals is None:
            # No ledger configured, so there is no bound path to insist on and
            # the base class's behaviour is the only one available.
            return super()._command_from_chat_reply(run, now=now)
        self._capture_chat_reply(run, now=now)
        return None

    def _capture_chat_reply(self, run: Run, *, now: int) -> None:
        """
        Copy anything said in the session into the bot's channel.

        So that refusing to treat prose as authorisation does not also throw
        the prose away — a person who typed "looks fine, merge it" said
        something worth keeping next to the question.

        :param run: The run parked on a person.
        :param now: Epoch seconds.
        """
        if self.messages is None or run.bot_id is None:
            return
        session_id = _asking_session(run)
        if session_id is None:
            return
        try:
            replies = self.omni.replies_after(session_id, barrier_marker(run.id))
        except Exception:  # noqa: BLE001 — a convenience must never cost an iteration
            # The question is already answerable from the CLI; this only mirrors
            # chatter into the channel, so a failure here is a non-event.
            _logger.debug("could not read replies for run %s", run.id[:12], exc_info=True)
            return
        seen = {
            message.payload.get("reply")
            for message in self.messages.thread(run.bot_id, run.id)
            if message.kind is MessageKind.HUMAN_MSG
        }
        for reply in replies:
            if reply in seen:
                continue
            self.messages.post(
                run.bot_id,
                "human:session",
                MessageKind.HUMAN_MSG,
                reply,
                now=now,
                payload={"reply": reply, "authorises": False},
                thread_id=run.id,
                run_id=run.id,
            )

    # ── mail and expiry ───────────────────────────────────────────

    def _deliver_mail(self, *, now: int) -> None:
        """
        Make event-driven bots with unread mail due.

        This is the insert half of the wake model. An ``on_message`` bot has no
        ``next_due_at`` at all — nothing is scheduling it, so time passing must
        never wake it — and a message arriving is the only thing that should.

        One query for the whole fleet, not one per bot: asking per bot is what
        turns a forty-bot roster slow exactly when it is busy.

        :param now: Epoch seconds.
        """
        if self.messages is None:
            return
        waiting = self.messages.waiting_recipients(now=now)
        if not waiting:
            return
        humans = self.messages.waiting_from_humans(now=now)
        for bot in self.bots.list(status=BotStatus.ACTIVE):
            if bot.next_due_at is not None or not is_event_driven(bot):
                # Already scheduled, or driven by a clock. Pulling a scheduled
                # bot forward on every message is how two bots that talk to each
                # other spin without a human ever being involved.
                #
                # A person is the exception, and the argument above is exactly
                # why: a human cannot spin a loop by talking, and being ignored
                # for fourteen minutes after saying something is the difference
                # between a colleague and a cron job. `waiting_humans` counts
                # only messages a person wrote.
                if bot.address not in humans:
                    continue
            if bot.address not in waiting:
                continue
            _logger.info(
                "bot %s has %d message(s) waiting; waking it", bot.slug, waiting[bot.address]
            )
            with suppress(ConcurrentTransition):
                wake_now(self.bots, bot, now=now, reason="event")

    def _expire_approvals(self, *, now: int) -> None:
        """
        Close out questions nobody answered, and pause the bots waiting on them.

        Expiry is never an approval — the hard gates forbid it, and a system
        that approves on silence makes a holiday into a blanket authorisation.
        The bot stops and says so, which is the failure mode a person can see.

        :param now: Epoch seconds.
        """
        if self.approvals is None:
            return
        for request in self.approvals.expire_due(now=now):
            _logger.warning(
                "approval %s for bot %s expired unanswered after %ds; pausing the bot",
                request.id[:12],
                request.bot_id,
                now - request.created_at,
            )
            if self.messages is not None:
                self.messages.post(
                    request.bot_id,
                    "system",
                    MessageKind.EVENT,
                    f"Question expired unanswered: {request.question}",
                    now=now,
                    payload={"approval_id": request.id, "outcome": "expired"},
                    thread_id=request.thread_id,
                    run_id=request.run_id,
                )
            bot = self.bots.get(request.bot_id)
            if bot is not None and bot.status is BotStatus.ACTIVE:
                self._pause(bot, now=now, reason="a question expired unanswered")

    # ── operator view ─────────────────────────────────────────────

    def fleet_tick(self, *, now: int | None = None) -> TickReport:
        """
        One full pass: mail, expiry, the run loop, and the alarm.

        Order matters. Mail is delivered *before* the scan, so a bot woken by a
        message is due in the same tick rather than the next one; expiry runs
        before it too, so a bot whose question timed out is paused rather than
        dispatched again with the question still open.

        :param now: Epoch seconds; defaults to the clock.
        :returns: What the pass did.
        """
        stamp = int(time.time()) if now is None else now
        self._steer_live_runs(now=stamp)
        self._deliver_mail(now=stamp)
        self._expire_approvals(now=stamp)
        self._retire_expired(now=stamp)
        report = self.tick(now=stamp)
        self._raise_alarms(now=stamp)
        return report

    def _take_messages(self, bot: Bot, *, now: int) -> list[str]:
        """
        Claim what a person said to an idle bot, for the brief about to be written.

        Acked here rather than after the session answers. The alternative is a
        message redelivered into every subsequent brief until the bot happens
        to reply to it, which reads to the body as the operator repeating
        themselves and is worse than losing it.

        :param bot: The bot about to run.
        :param now: Epoch seconds.
        :returns: What was said, oldest first.
        """
        if self.messages is None:
            return []
        said = []
        for delivery in self.messages.lease(bot.address, now=now, limit=10):
            if delivery.message.kind is not MessageKind.HUMAN_MSG:
                continue
            said.append(delivery.message.body)
            self.messages.ack(delivery.message.id, bot.address)
        return said

    def _steer_live_runs(self, *, now: int) -> None:
        """
        Hand anything a person said to the body that is already working.

        The half of the channel that made it a conversation rather than a log.
        Before this, a message to a busy bot sat in its inbox until the
        iteration finished, which is the one moment it is useless — the point
        of saying "no, not that branch" is to say it *while* the wrong branch
        is being cut.

        Leased rather than read, and acked only after the send returns, so a
        crash between the two redelivers rather than swallows. A vendor that is
        down leaves the message queued and the next tick tries again.

        Nothing here can authorise. The message goes in as text and the run
        stays exactly where it was: a bot parked on an approval is still parked
        after being spoken to, because an open question is answered on the
        bound path or not at all.

        :param now: Epoch seconds.
        """
        if self.messages is None:
            return
        for bot_id, run_id in self.bots.live_run_ids().items():
            bot = self.bots.get(bot_id)
            if bot is None:
                continue
            # The session is looked up *before* the lease, and that order is
            # the whole correctness of this method. Leasing first and then
            # finding no session strands the message: a lease hides it from
            # the wake scan too, so the bot is neither steered nor woken until
            # the lease expires — which is exactly the silence this feature
            # exists to remove.
            run = self.store.get_run(run_id)
            session = _asking_session(run) if run is not None else None
            if session is None:
                continue
            waiting = self.messages.lease(bot.address, now=now, limit=5)
            if not waiting:
                continue
            for delivery in waiting:
                if delivery.message.kind is not MessageKind.HUMAN_MSG:
                    # A bot-to-bot message is mail, not steering. It waits for
                    # the next iteration's brief like everything else.
                    continue
                try:
                    self.omni.send(session, _steer(delivery.message.body))
                except OmniError as exc:
                    _logger.warning(
                        "bot %s: could not steer run %s (%s); it stays queued",
                        bot.slug,
                        run_id[:12],
                        exc,
                    )
                    break
                self.messages.ack(delivery.message.id, bot.address)
                _logger.info("bot %s: steered run %s mid-iteration", bot.slug, run_id[:12])

    def _raise_alarms(self, *, now: int) -> None:
        """
        Report bots nothing will ever wake — once each, not once per tick.

        A lost succession is otherwise invisible: the bot is active, has no run,
        and nothing is scheduled to notice. But an unthrottled alarm is its own
        failure — the same line every ten seconds for a week is noise, and the
        night it finally means something nobody is reading it.

        :param now: Epoch seconds.
        """
        waiting = (
            {request.bot_id for request in self.approvals.pending()}
            if self.approvals is not None
            else set()
        )
        stalled = self.bots.stalled(now=now, excluding=waiting)
        current = {bot.id for bot in stalled}
        for bot in stalled:
            if bot.id in self._alarmed:
                continue
            _logger.error(
                "bot %s is active with no run, no next wake and nothing pending "
                "(last outcome %s, wake reason %s, idle since %ds ago) — its succession "
                "was lost; `army bots wake %s` restarts it",
                bot.slug,
                bot.last_outcome.value if bot.last_outcome else "none",
                bot.wake_reason.value if bot.wake_reason else "none",
                now - bot.updated_at,
                bot.slug,
            )
        for recovered in self._alarmed - current:
            _logger.info("bot %s is scheduled again", recovered)
        self._alarmed = current

    def _retire_expired(self, *, now: int) -> None:
        """
        Retire bots past their time-to-live, whatever they are waiting on.

        Expiry used to be checked only inside the due scan, so the bot most
        likely to have been forgotten — a child parked on a question nobody
        answered — was the one that could never reach the check.

        :param now: Epoch seconds.
        """
        for bot in self.bots.expired(now=now, limit=self.scan_limit):
            self._retire_if_expired(bot, now=now)


#: The only two outcomes a workload may declare for itself. Every other value
#: describes something the *supervisor* observed — a person, a vendor, a
#: failure — and letting a workload claim one would let it write scheduler
#: state it has no way to know is true.
_WORKLOAD_MAY_DECLARE = frozenset({RunOutcome.WORK_DONE.value, RunOutcome.NO_WORK.value})


def classify(run: Run, target: RunState) -> RunOutcome:
    """
    Say what an iteration achieved, in the terms the scheduler understands.

    Most moves classify themselves: parking on a person is ``BLOCKED``, a
    requeue is ``RATE_LIMITED``, a failure is an error. Exactly one case is
    genuinely ambiguous — an iteration that finished cleanly may have merged a
    change or may have looked and found nothing, and those two want opposite
    wake times. Only the workload knows which, and it says so by putting an
    ``outcome`` in the run's artifacts.

    The declaration is deliberately *not* consulted for the other moves. It is
    written in ``collect``, before the iteration is settled, so a run that then
    parks or fails still carries whatever ``collect`` believed. Letting that
    win would make blocking on a human count as an idle iteration — backing the
    bot off for having asked a question — and would reset the error streak on
    every failure.

    :param run: The run being settled.
    :param target: The state it is moving to.
    :returns: The outcome.
    """
    if target is RunState.WAITING_HUMAN:
        # Parked on a person. No next wake at all — the verdict's own
        # transaction is what schedules the bot again.
        return RunOutcome.BLOCKED
    if target is RunState.PAUSED:
        # A human stopped this branch. Nothing was learned about the mission,
        # so neither streak should move, and BLOCKED is the outcome that leaves
        # both alone.
        return RunOutcome.BLOCKED
    if target is RunState.FAILED:
        return RunOutcome.RETRYABLE_ERROR

    declared = run.artifacts.get("outcome")
    if isinstance(declared, str):
        # A workload may choose between "I did something" and "there was
        # nothing to do", and nothing else. It must not be able to declare
        # BLOCKED on a terminal move: that writes next_due_at = NULL on a bot
        # whose run has ended, and nothing would ever wake it again.
        if declared in _WORKLOAD_MAY_DECLARE:
            return RunOutcome(declared)
        _logger.warning(
            "run %s declared %r, which a workload may not choose (only %s); "
            "treating the iteration as having found no work",
            run.id[:12],
            declared,
            " or ".join(sorted(_WORKLOAD_MAY_DECLARE)),
        )
        # Fail toward NO_WORK, not WORK_DONE. The wrong guess here is the one
        # that keeps a continuous bot at its floor, which is the quota loop.
        return RunOutcome.NO_WORK
    return RunOutcome.WORK_DONE


def _describe(item: dict[str, Any]) -> str:
    """
    Name a work item for a log line without pasting its payload.

    A run's item can carry a whole file. ``%r`` on it turns one warning into a
    megabyte of log and buries the sentence that mattered.

    :param item: The work item.
    :returns: A short description.
    """
    for key in ("id", "task", "title", "path", "name"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return f"{key}={value[:80]!r}"
    return f"an item with keys {sorted(item)[:5]}"


def _steer(body: str) -> str:
    """
    Wrap what a person said so a body cannot mistake it for its own reasoning.

    A bare string arriving mid-turn reads like the agent's own note to self.
    Labelling it is the difference between "the operator is redirecting me" and
    a stray thought — and the last line is there because an agent that treats
    being spoken to as permission is the failure this whole channel is built to
    avoid.

    :param body: What the operator typed.
    :returns: The message to send into the session.
    """
    return (
        "## The operator is speaking to you, mid-iteration\n\n"
        f"{body}\n\n"
        "Treat this as a correction or a question about the work in progress. "
        "It is **not** an approval: any gate you are waiting on is still open "
        "and is answered elsewhere. Reply in this session; your reply is "
        "recorded in the bot's channel."
    )


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
