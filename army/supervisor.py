"""The loop: read the durable state, make one move, write it back.

There is no in-memory ``while True`` holding the loop's place. A tick reads
every non-terminal run from the store, advances each by at most one state, and
returns. Whether the previous tick ended by returning, by being killed, or by
the machine losing power makes no difference to the next one, because the rows
are the whole of what a tick needs to know.

That is also why restart recovery has no code of its own. A run interrupted in
``COLLECTING`` is still in ``COLLECTING``; the next tick collects it. The only
thing a crash can cost is an unrecorded external side effect, which is what the
effects journal in :mod:`army.store` is for.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from army.lanes import Lanes
from army.omni import BROWSER_PROFILE_LABEL, OmniClient, OmniError, barrier_marker
from army.state import Command, CommandKind, Run, RunState
from army.store import ConcurrentTransition, Store
from army.workload import Workload

_logger = logging.getLogger(__name__)

#: How many times an iteration may be dispatched before the supervisor stops
#: re-trying it. A run that fails this many times is failing for a reason a
#: retry will not fix, and spinning on it burns the same subscription quota the
#: rest of the loop needs.
MAX_ATTEMPTS = 3

#: How long a run may sit in a working state without its version moving before
#: the supervisor calls it stuck. A transient error retried forever looks
#: exactly like progress from outside — the loop keeps ticking and nothing ever
#: changes — so the retry has to be bounded by something. Generous, because
#: agent turns are genuinely slow; the states that wait on a person are exempt.
STALL_SECONDS = 3600

#: States that park on a person rather than on progress. Neither can be stuck,
#: and neither has a legal move to FAILED — a stall check that fired on one
#: would raise IllegalTransition and take the whole pass down with it.
_STALL_EXEMPT = frozenset({RunState.WAITING_HUMAN, RunState.PAUSED})

#: Artifact key holding the lanes this run's sessions were charged to, so the
#: release returns each charge to the lane it came from. Draining the busiest
#: lane instead moves a charge rather than removing it.
_CHARGED_LANES = "charged_lanes"

#: What a decision maps to when the workload does not say.
_DECISION_STATES = {
    "continue": RunState.CONTINUE,
    "paused": RunState.PAUSED,
    "completed": RunState.COMPLETED,
    "failed": RunState.FAILED,
}


@dataclass
class TickReport:
    """What one pass over the active runs did.

    :param advanced: Runs that changed state.
    :param unchanged: Runs that were looked at and left where they were, e.g.
        still collecting, or still waiting on a human.
    :param failed: Runs moved to ``FAILED`` this tick.
    :param started: Runs opened this tick.
    """

    advanced: int = 0
    unchanged: int = 0
    failed: int = 0
    started: int = 0

    def __str__(self) -> str:
        return (
            f"started={self.started} advanced={self.advanced} "
            f"unchanged={self.unchanged} failed={self.failed}"
        )


class Supervisor:
    """Drives runs of one workload.

    :param store: Where run state lives.
    :param omni: Client for the Omnigent server.
    :param workload: The job of work being iterated.
    :param lanes: Per-vendor admission control, or ``None`` for no limits.
    :param max_concurrent_runs: How many iterations may be in flight at once.
        A loop that opens runs faster than it finishes them just queues work
        against the same subscriptions.
    """

    def __init__(
        self,
        store: Store,
        omni: OmniClient,
        workload: Workload,
        *,
        lanes: Lanes | None = None,
        max_concurrent_runs: int = 3,
    ) -> None:
        self.store = store
        self.omni = omni
        self.workload = workload
        self.lanes = lanes
        self.max_concurrent_runs = max_concurrent_runs

    # ── one pass ──────────────────────────────────────────────────

    def tick(self, *, now: int | None = None) -> TickReport:
        """
        Make one pass over the active runs.

        :param now: Unix epoch seconds; defaults to the clock.
        :returns: What the pass did.
        """
        stamp = int(time.time()) if now is None else now
        report = TickReport()
        active = self._active_runs()

        for run in active:
            try:
                moved = self._advance(run, now=stamp)
            except ConcurrentTransition:
                # Someone else moved it. Their move is as good as ours would
                # have been, and re-reading here would race the same way.
                report.unchanged += 1
                continue
            except Exception:
                _logger.exception("run %s could not be advanced", run.id)
                try:
                    self._fail(run, "supervisor error", now=stamp)
                except Exception:
                    # Recording the failure failed too — a store that is down,
                    # or a subclass whose own write is what broke. Leaving the
                    # run exactly where it is costs one tick and loses nothing;
                    # letting this escape would end the pass and strand every
                    # run after it in the list.
                    _logger.exception(
                        "run %s could not even be marked failed; leaving it untouched", run.id
                    )
                report.failed += 1
                continue
            if moved is None:
                report.unchanged += 1
            elif moved.state is RunState.FAILED:
                report.failed += 1
            else:
                report.advanced += 1

        # A run parked on a person is *live* — it still owns its work item and
        # must not be opened twice — but it holds no vendor seat, no session
        # and no process. Counting it against the concurrency cap meant three
        # unanswered questions could stop a whole fleet, which is the opposite
        # of the doctrine the cap exists to serve: the limit is on iterations
        # *in flight against a subscription*, not on rows.
        working = [run for run in active if run.state is not RunState.WAITING_HUMAN]
        if len(working) < self.max_concurrent_runs:
            if self._open_run(now=stamp) is not None:
                report.started += 1
        return report

    def _open_run(self, *, now: int) -> Run | None:
        """
        Take one item off the workload's queue and open a run for it.

        :param now: Unix epoch seconds.
        :returns: The new run, or ``None`` when the queue is empty.
        """
        item = self.workload.acquire()
        if item is None:
            return None
        return self.store.create_run(Run.new(self.workload.name, item, now=now))

    # ── seams a bot fleet overrides ───────────────────────────────
    #
    # A supervisor driving one workload knows the answer to both of these
    # before it starts. A supervisor driving bots does not: each bot names its
    # own workload, and which runs are its business is a different query. They
    # are methods rather than constructor arguments so the subclass can answer
    # per run, which is the part that actually varies.

    def _active_runs(self) -> list[Run]:
        """
        The runs this supervisor is responsible for advancing.

        :returns: Non-terminal runs of this supervisor's workload.
        """
        return self.store.active_runs(workflow=self.workload.name)

    def _workload_for(self, run: Run) -> Workload:  # noqa: ARG002
        """
        The workload that owns one run.

        :param run: The run being advanced.
        :returns: Its workload.
        """
        return self.workload

    def _transition(self, run: Run, target: RunState, **kwargs: Any) -> Run:
        """
        Write one state change.

        Every move in this class goes through here, which is what lets a bot
        fleet make the *succession* atomic: ending an iteration and scheduling
        the bot's next wake are two writes, and a crash between them either
        repeats the iteration or loses it. A subclass overrides this once and
        both land in the same transaction.

        :param run: The run as read.
        :param target: Where to move it.
        :param kwargs: Passed to :meth:`army.store.Store.transition`.
        :returns: The run after its move.
        """
        return self.store.transition(run, target, **kwargs)

    # ── one move ──────────────────────────────────────────────────

    def _advance(self, run: Run, *, now: int) -> Run | None:
        """
        Advance one run by at most one state.

        :param run: The run as read, carrying the version to move from.
        :param now: Unix epoch seconds.
        :returns: The run after its move, or ``None`` if it stayed put.
        """
        # Waiting on a person is not being stuck, so the two states that do it
        # are exempt: WAITING_HUMAN parks on an answer, PAUSED parks on someone
        # deciding to restart the branch, and both are meant to outlast a night.
        # Everywhere else a version that has not moved in an hour means the
        # retry loop is not getting anywhere.
        if run.state not in _STALL_EXEMPT and now - run.updated_at > STALL_SECONDS:
            return self._fail(
                run,
                f"stuck in {run.state.value} for {(now - run.updated_at) // 60} minutes",
                now=now,
            )
        if run.state is RunState.READY:
            return self._dispatch(run, now=now)
        if run.state is RunState.DISPATCHING:
            # Reached only when a crash landed between the transition and the
            # dispatch itself. Re-dispatching is safe: session creation goes
            # through the workload, which owns its own idempotency.
            return self._dispatch_children(run, now=now)
        if run.state is RunState.COLLECTING:
            return self._collect(run, now=now)
        if run.state is RunState.EVALUATING:
            return self._ask(run, now=now)
        if run.state is RunState.WAITING_HUMAN:
            return self._apply_command(run, now=now)
        if run.state is RunState.PAUSED:
            return self._resume(run, now=now)
        return None

    def _resume(self, run: Run, *, now: int) -> Run | None:
        """Restart a paused branch, if a human has asked for it.

        Back to ``READY``, not to where it stopped: whatever it was collecting
        finished or died while it sat paused, so the only honest restart is a
        fresh iteration. The attempt counter resets with it — a branch a human
        deliberately restarted has not "failed twice already".

        :param run: The paused run.
        :param now: Unix epoch seconds.
        :returns: The run after resuming, or ``None`` if nothing asked it to.
        """
        command = self.store.next_command(run.id)
        if command is None:
            return None
        if command.kind is not CommandKind.RESUME:
            # Anything else aimed at a paused run is stale — most likely a
            # verdict that arrived after the branch was already stopped.
            # Consume it so it cannot wake the run later with the wrong answer.
            self._discard_stale(run, command, now=now)
            return None
        _logger.info("run %s resumed by owner", run.id)
        return self._transition(
            run, RunState.READY, attempt=0, terminal_reason=None, consume=command, now=now
        )

    def _discard_stale(self, run: Run, command: Command, *, now: int) -> None:
        """Consume a command that cannot apply where the run now is."""
        _logger.info(
            "run %s is %s; discarding a stale %s command",
            run.id,
            run.state.value,
            command.kind.value,
        )
        self.store.consume_command(command, now=now)

    def _dispatch(self, run: Run, *, now: int) -> Run | None:
        """Move ``READY`` to ``DISPATCHING``, respecting attempt limits and lanes."""
        if run.attempt >= MAX_ATTEMPTS:
            return self._fail(run, f"gave up after {run.attempt} attempts", now=now)
        if self.lanes is not None and not self.lanes.would_admit(
            _workload_lane(self._workload_for(run))
        ):
            # The lane this run will land on is full or cooling. Leaving it in
            # READY is the backpressure: it goes when that lane frees. Asking
            # "is any lane free" instead would admit a third Claude session
            # because Grok happens to be idle.
            return None
        moved = self._transition(run, RunState.DISPATCHING, attempt=run.attempt + 1, now=now)
        return self._dispatch_children(moved, now=now)

    def _omni_for(self, run: Run) -> OmniClient:
        """
        The client a workload gets, carrying this run's session labels.

        Every session a bot opens should name the browser that bot owns, and
        that is not a per-workload decision: the watcher set the label itself
        and the research workload — which nine of ten bots in the crypto
        example use — did not, so nine bots had no browser and nothing said so.
        Setting it here means a workload cannot forget.

        :param run: The run about to dispatch.
        :returns: The client, labelled when the run names a browser.
        """
        profile = str(run.artifacts.get("browser_profile") or "")
        if not profile:
            return self.omni
        return self.omni.with_labels({BROWSER_PROFILE_LABEL: profile})

    def _dispatch_children(self, run: Run, *, now: int) -> Run | None:
        """Start the workload's sessions and move on to collecting them."""
        try:
            sessions = self._workload_for(run).dispatch(run, self._omni_for(run))
        except OmniError as exc:
            if exc.is_transient:
                # Leave it in DISPATCHING; the next tick tries again and the
                # attempt counter still bounds how long that can go on.
                _logger.warning("dispatch for run %s hit a transient error: %s", run.id, exc)
                return None
            return self._fail(run, f"dispatch failed: {exc}", now=now)
        charged = self._charge_lanes(sessions)
        return self._transition(
            run,
            RunState.COLLECTING,
            outstanding=sessions,
            artifacts={**run.artifacts, _CHARGED_LANES: charged},
            now=now,
        )

    def _charge_lanes(self, session_ids: list[str]) -> list[str]:
        """Occupy the lane each session actually landed on.

        Asking the server which harness bound is the only honest way to do
        this: a per-session override can be refused, and the agent spec's own
        harness wins by default, so what the workload intended and what is
        running are not reliably the same. Charging by intent — or worse, by
        whichever lane happens to be emptiest — leaves the counters describing
        a fleet that does not exist.

        Best-effort. A lane that cannot be charged is a wrong number, not a
        reason to fail an iteration that has already started.

        :param session_ids: Sessions just dispatched.
        :returns: The harness each session landed on, so the release can return
            the charge to the same lane it came from.
        """
        charged: list[str] = []
        if self.lanes is None:
            return charged
        for session_id in session_ids:
            try:
                harness = self.omni.get_session(session_id).harness
            except OmniError as exc:
                _logger.warning("could not read the harness for %s: %s", session_id, exc)
                continue
            if harness is None:
                continue
            charged.append(harness)
            if not self.lanes.acquire(1, harness):
                _logger.warning(
                    "session %s runs on %r, which has no configured lane — "
                    "its concurrency is unbounded until army.toml names it",
                    session_id,
                    harness,
                )
        return charged

    def _release_lanes(self, run: Run) -> None:
        """Return this run's charges to the lanes they came from.

        Falls back to an unkeyed release only for a run charged before the
        lanes were recorded — an old row mid-flight across an upgrade.

        :param run: The run whose sessions have finished.
        """
        if self.lanes is None:
            return
        charged = run.artifacts.get(_CHARGED_LANES)
        if isinstance(charged, list) and charged:
            for harness in charged:
                self.lanes.release(1, str(harness))
            return
        self.lanes.release(len(run.outstanding))

    def _collect(self, run: Run, *, now: int) -> Run | None:
        """Gather finished work; stay in ``COLLECTING`` until it is all in."""
        limited = self._rate_limited_lane(run)
        if limited is not None:
            return self._requeue_rate_limited(run, limited, now=now)
        try:
            done, artifacts = self._workload_for(run).collect(run, self.omni)
        except OmniError as exc:
            if exc.is_transient:
                _logger.warning("collect for run %s hit a transient error: %s", run.id, exc)
                return None
            return self._fail(run, f"collect failed: {exc}", now=now)
        merged = {**run.artifacts, **artifacts}
        if not done:
            if merged == run.artifacts:
                return None
            # Partial progress is worth persisting: a crash mid-iteration then
            # costs the remaining children, not the ones already finished.
            return self._transition(run, RunState.COLLECTING, artifacts=merged, now=now)
        if self.lanes is not None:
            self._release_lanes(run)
        return self._transition(
            run, RunState.EVALUATING, artifacts=merged, outstanding=[], now=now
        )

    def _rate_limited_lane(self, run: Run) -> tuple[str, str, str] | None:
        """
        Find an outstanding session whose vendor refused it on quota.

        Without this the loop reads a quota refusal as finished work: the
        session is no longer ``running``, so the workload collects it, the
        reviewer reviews an empty branch, and the human is asked to approve
        something nobody did.

        :param run: The run being collected.
        :returns: ``(session_id, harness, phrase)`` for the first limited
            session, or ``None``. Best-effort — a session that cannot be read
            is not evidence of a limit.
        """
        if self.lanes is None:
            return None
        for session_id in run.outstanding:
            try:
                session = self.omni.get_session(session_id)
            except OmniError as exc:
                _logger.warning("could not read %s while collecting: %s", session_id, exc)
                continue
            if session.status == "running" or session.harness is None:
                continue
            phrase = self.lanes.limit_phrase(session.tail)
            if phrase is not None:
                return session_id, session.harness, phrase
        return None

    def _requeue_rate_limited(
        self, run: Run, limited: tuple[str, str, str], *, now: int
    ) -> Run | None:
        """
        Cool the vendor's lane and put the run back in the queue.

        Back to ``READY`` rather than ``FAILED``: nothing went wrong with the
        work, a subscription simply ran out for a while. The cooled lane is
        what stops it going straight back to the same vendor —
        :meth:`_dispatch` returns before incrementing the attempt counter when
        no lane has capacity, so a long limit costs waiting rather than
        attempts.

        :param run: The run whose session was refused.
        :param limited: ``(session_id, harness, phrase)`` from
            :meth:`_rate_limited_lane`.
        :param now: Unix epoch seconds.
        :returns: The requeued run, or ``None`` if another writer won the race.
        """
        session_id, harness, phrase = limited
        if self.lanes is not None:
            # Best-effort and in-memory. A subclass may also have recorded the
            # limit somewhere that survives a restart, which is what actually
            # holds the lane closed; this only shapes the current process.
            self.lanes.rate_limited(harness)
        self._release_lanes(run)
        _logger.warning(
            "run %s: %s hit a %r limit on session %s — cooling that lane and requeueing",
            run.id,
            harness,
            phrase,
            session_id,
        )
        return self._transition(run, RunState.READY, outstanding=[], now=now)

    def _ask(self, run: Run, *, now: int) -> Run | None:
        """Put the question to the human and park on the answer."""
        question, options, evidence = self._workload_for(run).evaluate(run)
        session_id = _asking_session(run)
        if session_id is None:
            return self._fail(run, "no session to raise the approval on", now=now)
        try:
            approval_id = self.omni.ask(run.id, session_id, question, options, evidence=evidence)
        except OmniError as exc:
            if exc.is_transient:
                _logger.warning("could not raise approval for run %s: %s", run.id, exc)
                return None
            return self._fail(run, f"could not ask: {exc}", now=now)
        return self._transition(
            run,
            RunState.WAITING_HUMAN,
            artifacts={**run.artifacts, "question": question, "options": options},
            approval_id=approval_id,
            now=now,
        )

    def _apply_command(self, run: Run, *, now: int) -> Run | None:
        """Consume a durable answer, if one has arrived, and act on it.

        This is the step the whole design exists for. Nothing here resolves a
        future or wakes a coroutine — it reads a row that says what the human
        decided, which is a thing that survives the process they told it to.
        """
        command = self.store.next_command(run.id)
        if command is None:
            command = self._command_from_chat_reply(run, now=now)
        if command is None:
            return None
        if command.kind is CommandKind.CANCEL:
            return self._transition(
                run, RunState.FAILED, terminal_reason="cancelled", consume=command, now=now
            )
        if command.kind is CommandKind.PAUSE:
            return self._transition(
                run,
                RunState.PAUSED,
                terminal_reason="paused by owner",
                consume=command,
                now=now,
            )
        decision, reason = self._workload_for(run).apply(run, command.kind.value, command.payload)
        target = _DECISION_STATES.get(decision)
        if target is None:
            return self._fail(run, f"workload returned an unknown decision {decision!r}", now=now)
        return self._transition(run, target, terminal_reason=reason, consume=command, now=now)

    def _command_from_chat_reply(self, run: Run, *, now: int) -> Command | None:
        """
        Turn a reply typed into the session into a durable command.

        The question was posted into the session, and the Omnigent web UI is
        already reachable from a phone — so a reply typed there is the mobile
        answer path, with no second service to run or authenticate. Recording
        it as a command rather than acting on it directly keeps one answer
        mechanism: whether it arrived from a terminal or a phone, it is the
        same row, consumed exactly once.

        Matching is deliberately narrow. The reply must contain one of the
        offered options and no other, so "merge" answers and "merge or iterate,
        I can't decide" does not — an ambiguous answer to a gate is not an
        answer. Anything unrecognised is left alone, so the run stays parked
        and a person can say it again more clearly.

        :param run: The run parked on a human.
        :param now: Unix epoch seconds.
        :returns: The recorded command, or ``None`` when nothing decisive was
            said.
        """
        session_id = _asking_session(run)
        if session_id is None:
            return None
        try:
            replies = self.omni.replies_after(session_id, barrier_marker(run.id))
        except OmniError as exc:
            # Expected: the server is briefly unreachable. Next tick retries.
            _logger.warning("could not read replies for run %s: %s", run.id, exc)
            return None
        except Exception:
            # Anything else is a bug here, and this path is only a convenience
            # on top of `army approve` — it must not cost a run. Without this,
            # a fault reaches tick()'s catch-all and fails an iteration that
            # was merely waiting to be answered.
            _logger.exception("reading replies for run %s failed", run.id)
            return None

        options = [str(o) for o in (run.artifacts.get("options") or [])]
        for reply in replies:
            matched = _options_named_in(reply, options)
            if len(matched) > 1:
                # Two options named: "merge or iterate?" is a question, not an
                # answer. Leave it parked and let them say it again.
                continue
            if len(matched) == 1:
                option, negated = matched[0]
                if negated:
                    _logger.info("run %s declined in chat: not %s", run.id, option)
                    return self.answer(run.id, CommandKind.DENY, {}, now=now)
                _logger.info("run %s answered in chat: %s", run.id, option)
                return self.answer(run.id, CommandKind.APPROVE, {"choice": option}, now=now)
            # Only fall back to bare refusal words when no option was named, and
            # never let one double as an option — "stop" is a deny word in
            # general and a legitimate choice in this workload.
            offered = {option.lower() for option in options}
            words = _words(reply)
            if any(word in words for word in _DENY_WORDS - offered):
                return self.answer(run.id, CommandKind.DENY, {}, now=now)
        return None

    def _fail(self, run: Run, reason: str, *, now: int) -> Run | None:
        """Move a run to ``FAILED``, tolerating a race with another mover."""
        if self.lanes is not None and run.outstanding:
            self._release_lanes(run)
        try:
            return self._transition(
                run, RunState.FAILED, terminal_reason=reason, outstanding=[], now=now
            )
        except ConcurrentTransition:
            return None

    # ── answering ─────────────────────────────────────────────────

    def resume(self, run_id: str, *, now: int | None = None) -> Command:
        """
        Ask a paused branch to start again.

        :param run_id: Run to restart.
        :param now: Unix epoch seconds; defaults to the clock.
        :returns: The recorded command.
        """
        stamp = int(time.time()) if now is None else now
        return self.store.record_command(Command.new(run_id, CommandKind.RESUME, {}, now=stamp))

    def answer(
        self,
        run_id: str,
        kind: CommandKind,
        payload: dict[str, Any] | None = None,
        *,
        now: int | None = None,
    ) -> Command:
        """
        Record a human decision as a durable command.

        Recording is all this does. The supervisor applies it on its next tick,
        so an answer given while nothing is running is not lost — it is simply
        applied later.

        :param run_id: Run being answered.
        :param kind: The decision.
        :param payload: Anything the answer carried.
        :param now: Unix epoch seconds; defaults to the clock.
        :returns: The recorded command.
        """
        stamp = int(time.time()) if now is None else now
        return self.store.record_command(Command.new(run_id, kind, payload or {}, now=stamp))


#: Words that refuse without naming an option. Any that is also an offered
#: option is dropped at match time — a workload may legitimately offer "stop".
_DENY_WORDS: frozenset[str] = frozenset({"deny", "reject", "decline", "no", "stop", "abort"})

#: Words that invert the option immediately after them.
_NEGATIONS: frozenset[str] = frozenset({"not", "dont", "don't", "never", "no", "avoid", "without"})


def _words(text: str) -> list[str]:
    """Split a reply into comparable words, dropping punctuation."""
    return re.findall(r"[a-z0-9'-]+", text.lower())


def _options_named_in(reply: str, options: list[str]) -> list[tuple[str, bool]]:
    """
    Find which offered options a reply names, and whether each was negated.

    Whole words only. Substring matching is what makes a gate fail open: "do
    not merge this" contains "merge", and a gate that reads that as approval is
    worse than one that never asks.

    :param reply: What the human typed.
    :param options: The options that were offered.
    :returns: ``(option, negated)`` for each option named, at most once each.
    """
    words = _words(reply)
    found: list[tuple[str, bool]] = []
    for option in options:
        needle = _words(option)
        if not needle:
            continue
        for start in range(len(words) - len(needle) + 1):
            if words[start : start + len(needle)] != needle:
                continue
            negated = start > 0 and words[start - 1] in _NEGATIONS
            found.append((option, negated))
            break
    return found


def _workload_lane(workload: Workload) -> str | None:
    """The vendor a workload dispatches to, when it commits to one.

    Optional by design: a workload that spreads across vendors, or picks per
    item, has no single answer and gets the any-lane check instead.

    :param workload: The workload about to dispatch.
    :returns: A harness id, or ``None`` when the workload does not declare one.
    """
    harness = getattr(workload, "harness", None)
    return harness if isinstance(harness, str) and harness else None


def _asking_session(run: Run) -> str | None:
    """
    Pick the session an iteration's approval should be raised on.

    Prefers a session the workload named in its artifacts, so the question
    appears where the work happened; falls back to whatever the run last
    dispatched.

    :param run: The run about to ask.
    :returns: A session id, or ``None`` when the run has no session at all.
    """
    named = run.artifacts.get("approval_session_id")
    if isinstance(named, str) and named:
        return named
    sessions = run.artifacts.get("sessions")
    if isinstance(sessions, list) and sessions and isinstance(sessions[0], str):
        return sessions[0]
    return run.outstanding[0] if run.outstanding else None
