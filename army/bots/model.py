"""What a bot is: identity, mission, wake policy, and the outcome of a run.

A bot owns no process between iterations. It owns rows. Everything here is
either a row's shape or a pure function over one, so the whole file is safe to
import from a test with no database and no server.

Two vocabularies live here and they are deliberately kept apart. A bot's own
:class:`BotStatus` is four states and only a human moves between them. What the
bot is *doing* is :class:`DerivedStatus`, computed at read time from the runs,
gates and approvals that already exist. Storing the second would give the
system two state machines that can disagree, and the one that disagrees is
always the one on the screen.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

#: Bots may not create grandchildren. A depth-2 tree with the default fan-out
#: is already nine bodies, which is the whole box.
MAX_DEPTH = 2

#: Children one parent may hold at once.
MAX_FANOUT = 3

#: Active bots across the whole fleet, matching the 5–10 concurrent-session
#: ceiling the deployment was sized for.
MAX_ACTIVE_BOTS = 10


class BotStatus(str, Enum):
    """A bot's own lifecycle. Only a human moves it.

    Four states, on purpose. Everything operational is derived — see
    :class:`DerivedStatus`.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


#: The moves a human may make. A retired bot stays retired: its runs and
#: messages remain readable, but reviving one would silently resume a mission
#: whose world has moved on.
LEGAL_BOT_MOVES: dict[BotStatus, frozenset[BotStatus]] = {
    BotStatus.DRAFT: frozenset({BotStatus.ACTIVE, BotStatus.RETIRED}),
    BotStatus.ACTIVE: frozenset({BotStatus.PAUSED, BotStatus.RETIRED}),
    BotStatus.PAUSED: frozenset({BotStatus.ACTIVE, BotStatus.RETIRED}),
    BotStatus.RETIRED: frozenset(),
}


class IllegalBotMove(RuntimeError):
    """Raised when a lifecycle move is not in :data:`LEGAL_BOT_MOVES`."""


def assert_legal_bot_move(source: BotStatus, target: BotStatus) -> None:
    """
    Reject a lifecycle move the model does not define.

    :param source: Where the bot is now.
    :param target: Where it would go.
    :raises IllegalBotMove: If the move is not declared.
    """
    if target not in LEGAL_BOT_MOVES[source]:
        raise IllegalBotMove(f"{source.value} -> {target.value} is not a legal move for a bot")


class RunOutcome(str, Enum):
    """How an iteration turned out, in the only terms the scheduler understands.

    This is the signal the whole wake policy is built on. Without it every
    terminated run looks like work was done, so a continuous bot wakes at its
    floor forever — the quota feedback loop the design exists to prevent.
    """

    WORK_DONE = "work_done"
    NO_WORK = "no_work"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    RETRYABLE_ERROR = "retryable_error"


class DerivedStatus(str, Enum):
    """What a bot is doing right now, computed rather than stored.

    Nine values, because the operator's question is "which one is stuck?" and
    an honest answer distinguishes a bot waiting on a person from one waiting
    on a vendor from one that simply has nothing to do yet.
    """

    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    WAITING_RESOURCE = "waiting_resource"
    BACKING_OFF = "backing_off"
    SCHEDULED = "scheduled"
    DUE = "due"
    WAITING_EVENT = "waiting_event"
    MANUAL = "manual"
    BLOCKED = "blocked"
    #: Not operational at all — the bot is draft, paused or retired, so no
    #: schedule applies and none of the nine questions above are meaningful.
    INACTIVE = "inactive"


class WakeKind(str, Enum):
    """Which rule decides when a bot is next due."""

    RRULE = "rrule"
    CONTINUOUS = "continuous"
    ON_MESSAGE = "on_message"
    MANUAL = "manual"


#: Why the bot's current ``next_due_at`` is what it is. This is what keeps
#: three unrelated situations from collapsing into one display state: a manual
#: bot, an event-driven bot and a bot blocked on a person all have no next
#: wake, and only one of them is stuck.
class WakeReason(str, Enum):
    """What set the current wake time."""

    SCHEDULE = "schedule"
    BACKOFF = "backoff"
    EVENT = "event"
    MANUAL = "manual"
    HUMAN = "human"


class InvalidWakePolicy(ValueError):
    """Raised when a wake spec cannot be honoured as written."""


@dataclass(frozen=True)
class WakePolicy:
    """When a bot should next be considered for dispatch.

    One spec covers both scheduled and continuous bots, because two code paths
    means two failure modes and a continuous bot that burns a subscription
    doing nothing.

    :param kind: Which rule applies.
    :param rrule: iCalendar recurrence string, for :attr:`WakeKind.RRULE`.
    :param min_interval_s: Floor between iterations for a continuous bot. This
        is not zero — continuous is not zero-delay scheduling, that is a quota
        feedback loop.
    :param base_s: First backoff step after an idle iteration.
    :param max_s: Ceiling the backoff decays to.
    :param factor: Multiplier per consecutive idle iteration.
    :param jitter: Fraction of the computed delay to spread randomly, so a
        fleet that went idle together does not wake together.
    :param sources: For :attr:`WakeKind.ON_MESSAGE`, the senders that wake it.
        Patterns are ``bot:*``, ``human:*`` or an exact address.
    :param precondition: Name of a registered LLM-free check. Mandatory for a
        continuous bot: without one, finding out there is nothing to do costs a
        full vendor turn every time.
    :param anchor: Epoch seconds an rrule is phased from, fixed at activation.
        Without it the rule re-phases to whenever the last iteration happened,
        so "daily at nine" drifts by one runtime every day, forever.
    """

    kind: WakeKind
    rrule: str | None = None
    anchor: int | None = None
    min_interval_s: int = 60
    base_s: int = 60
    max_s: int = 3600
    factor: float = 2.0
    jitter: float = 0.1
    sources: tuple[str, ...] = ()
    precondition: str | None = None

    def __post_init__(self) -> None:
        if self.kind is WakeKind.RRULE and not self.rrule:
            raise InvalidWakePolicy("an rrule wake policy needs an 'rrule' string")
        if self.kind is WakeKind.CONTINUOUS and not self.precondition:
            # The one rule in this file with teeth. A continuous bot with no
            # cheap check is a bot that pays a vendor turn to learn nothing,
            # every interval, forever.
            raise InvalidWakePolicy(
                "a continuous wake policy must declare a 'precondition'; "
                "without one the bot spends a vendor turn to discover it has no work"
            )
        if self.min_interval_s < 1 or self.base_s < 1 or self.max_s < 1:
            raise InvalidWakePolicy("wake intervals are in seconds and must be positive")
        if self.max_s < self.base_s:
            raise InvalidWakePolicy("backoff 'max_s' cannot be below 'base_s'")
        if self.factor < 1.0:
            raise InvalidWakePolicy("a backoff factor below 1 speeds up when idle")
        if not 0.0 <= self.jitter <= 1.0:
            raise InvalidWakePolicy("jitter is a fraction between 0 and 1")

    @staticmethod
    def from_dict(spec: dict[str, Any]) -> WakePolicy:
        """
        Build a policy from its JSON/YAML form.

        :param spec: The mapping, e.g. ``{"kind": "rrule", "rrule": "FREQ=DAILY"}``.
        :returns: The policy.
        :raises InvalidWakePolicy: If the kind is unknown or the spec is
            inconsistent.
        """
        raw_kind = str(spec.get("kind", "")).strip().lower()
        try:
            kind = WakeKind(raw_kind)
        except ValueError as exc:
            known = ", ".join(k.value for k in WakeKind)
            raise InvalidWakePolicy(
                f"unknown wake kind {raw_kind!r}; expected one of {known}"
            ) from exc
        backoff = spec.get("backoff") or {}
        if not isinstance(backoff, dict):
            raise InvalidWakePolicy("'backoff' must be a mapping")
        sources = spec.get("from") or spec.get("sources") or ()
        if isinstance(sources, str):
            sources = (sources,)
        return WakePolicy(
            kind=kind,
            rrule=spec.get("rrule"),
            anchor=int(spec["anchor"]) if spec.get("anchor") is not None else None,
            min_interval_s=int(spec.get("min_interval_s", 60)),
            base_s=int(backoff.get("base_s", 60)),
            max_s=int(backoff.get("max_s", 3600)),
            factor=float(backoff.get("factor", 2.0)),
            jitter=float(backoff.get("jitter", 0.1)),
            sources=tuple(str(s) for s in sources),
            precondition=spec.get("precondition"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Render the policy back to its JSON form, dropping defaults it does not use."""
        spec: dict[str, Any] = {"kind": self.kind.value}
        if self.kind is WakeKind.RRULE:
            spec["rrule"] = self.rrule
            if self.anchor is not None:
                spec["anchor"] = self.anchor
        if self.kind is WakeKind.CONTINUOUS:
            spec["min_interval_s"] = self.min_interval_s
            spec["backoff"] = {
                "base_s": self.base_s,
                "max_s": self.max_s,
                "factor": self.factor,
                "jitter": self.jitter,
            }
        if self.kind is WakeKind.ON_MESSAGE:
            spec["from"] = list(self.sources)
        if self.precondition:
            spec["precondition"] = self.precondition
        return spec

    def phased_at(self, when: int) -> WakePolicy:
        """
        The same policy, with its recurrence pinned to *when*.

        Called once, at activation. Re-pinning later would reintroduce the
        drift this exists to stop.

        :param when: Epoch seconds to phase from.
        :returns: A policy carrying the anchor, or this one if it already has
            an anchor or is not a recurrence.
        """
        if self.kind is not WakeKind.RRULE or self.anchor is not None:
            return self
        return replace(self, anchor=when)

    def accepts_sender(self, author: str) -> bool:
        """
        Whether a message from *author* should wake this bot.

        :param author: An address like ``"bot:abc"`` or ``"human:dpal"``.
        :returns: ``True`` when the policy listens to that sender. A policy
            with no sources listens to everyone.
        """
        if self.kind is not WakeKind.ON_MESSAGE:
            return False
        if not self.sources:
            return True
        for pattern in self.sources:
            if pattern == author or pattern == "*":
                return True
            prefix, star, _ = pattern.partition("*")
            if star and author.startswith(prefix):
                return True
        return False


@dataclass
class Bot:
    """One bot: a durable configuration row with an identity.

    ``persona`` and ``mission`` are the whole of "personality" — prompt text
    injected into every run, not a process attribute and not code. That is what
    makes a bot addable at runtime as pure data.

    :param id: Stable bot id.
    :param slug: Short addressable name, unique across the fleet.
    :param display_name: What a person calls it.
    :param title: Role label, e.g. ``"Research Analyst"``.
    :param persona: The standing role, injected into every run.
    :param mission: The objective it pursues, in prose.
    :param workload: Dotted ``module:Class`` path to its workload.
    :param workload_config: Constructor keyword arguments for that workload.
    :param harness: Preferred harness id, or ``None`` to let the router pick.
    :param wake: When it is next considered for dispatch.
    :param status: Its own lifecycle state.
    :param next_due_at: Epoch seconds of the next wake, or ``None`` for a bot
        nothing is scheduling — which is normal for manual and event-driven
        bots and a problem only when :attr:`wake_reason` says ``human``.
    :param wake_reason: What set :attr:`next_due_at`.
    :param idle_streak: Consecutive iterations that found nothing to do.
    :param error_streak: Consecutive failing iterations. Survives runs, which
        ``Run.attempt`` does not, so a bot failing every iteration is bounded.
    :param last_outcome: The previous run's classification.
    :param current_revision_id: The definition a new run pins.
    :param parent_bot_id: Who created it, for a spawned bot.
    :param root_bot_id: Lineage root, used by the fan-out cap.
    :param depth: Distance from the root; capped at :data:`MAX_DEPTH`.
    :param expires_at: When a spawned bot retires on its own.
    :param created_by: ``"human:<id>"`` or ``"bot:<id>"``.
    :param workspace: Git worktree path, which is also its docs repo.
    :param browser_profile: Electron partition key / browser profile directory.
    :param docs_ref: Where its documentation lives.
    :param paused_reason: Why the system paused it, when the system did. A bot
        paused by a transient fault otherwise looks exactly like one a person
        stopped on purpose, so nobody restarts it once the fault is fixed.
    :param version: CAS fencing token, same discipline as ``Run.version``.
    :param created_at: Epoch seconds.
    :param updated_at: Epoch seconds of the last write.
    """

    id: str
    slug: str
    display_name: str
    persona: str
    mission: str
    workload: str
    wake: WakePolicy
    status: BotStatus
    created_by: str
    created_at: int
    updated_at: int
    title: str | None = None
    workload_config: dict[str, Any] = field(default_factory=dict)
    harness: str | None = None
    next_due_at: int | None = None
    wake_reason: WakeReason | None = None
    idle_streak: int = 0
    error_streak: int = 0
    last_outcome: RunOutcome | None = None
    current_revision_id: str | None = None
    parent_bot_id: str | None = None
    root_bot_id: str | None = None
    depth: int = 0
    expires_at: int | None = None
    workspace: str | None = None
    browser_profile: str | None = None
    docs_ref: str | None = None
    paused_reason: str | None = None
    version: int = 0

    @staticmethod
    def new(
        slug: str,
        *,
        display_name: str | None = None,
        persona: str,
        mission: str,
        workload: str,
        wake: WakePolicy,
        created_by: str,
        now: int,
        **extra: Any,
    ) -> Bot:
        """
        Define a bot, in :attr:`BotStatus.DRAFT`.

        Draft rather than active on purpose: activation is the human's move,
        which is what makes runaway self-replication structurally impossible
        rather than merely discouraged.

        :param slug: Addressable name.
        :param display_name: Human-facing name; defaults to *slug*.
        :param persona: Standing role text.
        :param mission: Objective.
        :param workload: Dotted path to its workload.
        :param wake: Wake policy.
        :param created_by: Who defined it.
        :param now: Epoch seconds.
        :param extra: Any other field on :class:`Bot`.
        :returns: The bot, not yet persisted.
        """
        bot_id = extra.pop("id", None) or uuid.uuid4().hex
        return Bot(
            id=bot_id,
            slug=slug,
            display_name=display_name or slug,
            persona=persona,
            mission=mission,
            workload=workload,
            wake=wake,
            status=BotStatus.DRAFT,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            # A top-level bot is its own lineage root, so the fan-out cap has
            # something to count against even before anything is spawned.
            root_bot_id=extra.pop("root_bot_id", None) or bot_id,
            **extra,
        )

    @property
    def address(self) -> str:
        """This bot's message address, e.g. ``"bot:3f9c1ab…"``."""
        return f"bot:{self.id}"

    @property
    def is_operational(self) -> bool:
        """Whether the scheduler considers this bot at all."""
        return self.status is BotStatus.ACTIVE

    def definition(self) -> dict[str, Any]:
        """
        The snapshot pinned into a :class:`BotRevision`.

        Only the fields that change what a run *does*. Scheduler bookkeeping is
        excluded: a bot that merely backed off has not been redefined, and
        making it look that way would fill the history with noise.

        :returns: A JSON-serialisable definition.
        """
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "title": self.title,
            "persona": self.persona,
            "mission": self.mission,
            "workload": self.workload,
            "workload_config": self.workload_config,
            "harness": self.harness,
            "wake": self.wake.to_dict(),
            "workspace": self.workspace,
            "browser_profile": self.browser_profile,
            "docs_ref": self.docs_ref,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class BotRevision:
    """An immutable snapshot of a bot's definition.

    Every run pins one. Two reasons: a bot invented at runtime has no YAML file
    to point at, and without a pinned definition, editing a persona silently
    rewrites the meaning of that bot's own history.

    :param id: Stable revision id.
    :param bot_id: Which bot.
    :param rev: Monotonic revision number, starting at 1.
    :param definition: The full snapshot, from :meth:`Bot.definition`.
    :param created_by: Who made the edit.
    :param created_at: Epoch seconds.
    """

    id: str
    bot_id: str
    rev: int
    definition: dict[str, Any]
    created_by: str
    created_at: int

    @staticmethod
    def of(bot: Bot, rev: int, *, created_by: str, now: int) -> BotRevision:
        """
        Snapshot a bot's current definition.

        :param bot: The bot to snapshot.
        :param rev: The revision number this becomes.
        :param created_by: Who caused it.
        :param now: Epoch seconds.
        :returns: The revision, not yet persisted.
        """
        return BotRevision(
            id=uuid.uuid4().hex,
            bot_id=bot.id,
            rev=rev,
            definition=bot.definition(),
            created_by=created_by,
            created_at=now,
        )


def derive_status(
    bot: Bot,
    *,
    live_run_state: str | None,
    lane_blocked: bool,
    now: int,
) -> DerivedStatus:
    """
    Work out what a bot is doing, from state that already exists elsewhere.

    Nothing here is stored. That is the point: a derived status cannot disagree
    with the runs it describes, and two state machines that can disagree is the
    3am page.

    Order matters. A live run outranks any schedule, and a bot blocked on a
    person outranks a lane that happens to be cool — otherwise the operator
    chases the vendor instead of answering the question.

    :param bot: The bot.
    :param live_run_state: ``RunState`` value of its non-terminal run, or
        ``None`` when it has none.
    :param lane_blocked: Whether its vendor lane is at cap or cooling.
    :param now: Epoch seconds.
    :returns: The derived status.
    """
    if bot.status is not BotStatus.ACTIVE:
        return DerivedStatus.INACTIVE
    if live_run_state is not None:
        if live_run_state == "waiting_human":
            return DerivedStatus.WAITING_HUMAN
        return DerivedStatus.RUNNING
    if bot.next_due_at is None:
        # Three unrelated situations share "no next wake". Only the third is a
        # problem, and wake_reason is what tells them apart.
        if bot.wake.kind is WakeKind.ON_MESSAGE:
            return DerivedStatus.WAITING_EVENT
        if bot.wake.kind is WakeKind.MANUAL:
            return DerivedStatus.MANUAL
        return DerivedStatus.BLOCKED
    if lane_blocked:
        return DerivedStatus.WAITING_RESOURCE
    if bot.next_due_at > now:
        return DerivedStatus.BACKING_OFF if bot.idle_streak > 0 else DerivedStatus.SCHEDULED
    return DerivedStatus.DUE


def dumps(value: Any) -> str:
    """Encode a JSON column with stable key order so rows diff cleanly."""
    return json.dumps(value, sort_keys=True)
