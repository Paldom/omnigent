"""Seed a fleet that shows every capability at once, for the walkthrough.

Nine bots, deliberately in nine different states, plus a real question waiting
on a person, a real proposal from one bot to another, a spent budget and a
cooling vendor. Everything here goes through the same code paths the loop uses
— nothing is faked into the database.
"""

from __future__ import annotations

# The Offline fake below implements the client protocol, so its parameter names
# are the contract even where the body ignores them.
# ruff: noqa: ARG002
import sys
import time
from pathlib import Path

# Run from the repository root, so `army` imports without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from army.bots.approvals import ApprovalStore
from army.bots.budget import BudgetStore
from army.bots.memory import remember
from army.bots.messages import MessageKind, MessageStore
from army.bots.model import Bot, BotStatus, RunOutcome, WakeKind, WakePolicy
from army.bots.registry import WorkloadRegistry
from army.bots.schedule import Wake, first_wake
from army.bots.spawn import SpawnStore
from army.bots.store import BotStore
from army.bots.supervisor import BotSupervisor
from army.bots.workspace import Workspace
from army.omni import Session
from army.state import Run, RunState
from army.store import Store

DB = Path(sys.argv[1])
NOW = int(time.time())


class Offline:
    """Stands in for the server, so the demo needs no vendor."""

    def create_session(self, agent_id: str, **kwargs: object) -> str:
        return "conv_demo"

    def send(self, session_id: str, text: str) -> None:
        return None

    def get_session(self, session_id: str) -> Session:
        return Session(session_id, None, "idle", None, [], "")

    def replies_after(self, session_id: str, marker: str) -> list[str]:
        return []

    def was_told(self, session_id: str, text: str) -> bool:
        return True

    def find_session(self, title: str) -> str | None:
        return None

    def resolve_agent(self, name: str) -> str:
        return f"ag_{name}"

    def ask(self, run_id, session_id, message, options, *, evidence=None) -> str:
        return f"barrier_{run_id}"


store = Store(DB)
bots = BotStore(store)
messages = MessageStore(bots)
approvals = ApprovalStore(bots, messages=messages)
budgets = BudgetStore(bots)
spawns = SpawnStore(bots, budgets)
workspace = Workspace(bots, root=DB.parent / "bots")

HEARTBEAT = "army.bots.workloads.heartbeat:HeartbeatWorkload"


def define(slug: str, title: str, mission: str, persona: str, wake: WakePolicy, **extra) -> Bot:
    bot = Bot.new(
        slug,
        display_name=slug,
        title=title,
        persona=persona,
        mission=mission,
        workload=HEARTBEAT,
        wake=wake,
        created_by="human:dpal",
        now=NOW,
        **extra,
    )
    bots.create(bot)
    return bot


def continuous(**kw) -> WakePolicy:
    kw.setdefault("precondition", "always")
    return WakePolicy(kind=WakeKind.CONTINUOUS, **kw)


# ── the roster ────────────────────────────────────────────────────
scout = define(
    "scout",
    "Research Analyst",
    "Watch the eval set for regressions and propose a patch when recall drops.",
    "You read carefully and you do not guess.",
    continuous(min_interval_s=60),
    harness="claude-native",
)
librarian = define(
    "librarian",
    "Indexer",
    "Index new papers into the docs repository and summarise what changed.",
    "You are exhaustive and you cite everything.",
    continuous(min_interval_s=120),
)
treasurer = define(
    "treasurer",
    "Spend Approver",
    "Renew the domains and the two subscriptions this fleet depends on.",
    "You never spend without a signed owner grant.",
    WakePolicy(kind=WakeKind.RRULE, rrule="FREQ=DAILY;BYHOUR=9"),
)
harvester = define(
    "harvester",
    "Collector",
    "Pull the overnight benchmark runs and file the numbers.",
    "You collect; you do not interpret.",
    continuous(min_interval_s=90),
    harness="claude-native",
)
groundskeeper = define(
    "groundskeeper",
    "Janitor",
    "Prune stale worktrees and close branches nobody merged.",
    "You are conservative about deleting things.",
    continuous(min_interval_s=300),
)
cartographer = define(
    "cartographer",
    "Reporter",
    "Draw the weekly map of what the fleet did.",
    "You write for someone who was away all week.",
    WakePolicy(kind=WakeKind.RRULE, rrule="FREQ=DAILY;BYHOUR=9"),
)
sentry = define(
    "sentry",
    "Listener",
    "Answer questions the other bots address to you.",
    "You reply briefly and you say when you do not know.",
    WakePolicy(kind=WakeKind.ON_MESSAGE),
)
tinker = define(
    "tinker",
    "Odd Jobs",
    "Whatever is asked, when it is asked.",
    "You do one thing and stop.",
    WakePolicy(kind=WakeKind.MANUAL),
)
prospector = define(
    "prospector",
    "Proposed",
    "Find datasets worth indexing.",
    "You are speculative and you say so.",
    WakePolicy(kind=WakeKind.MANUAL),
)

for bot in (scout, librarian, treasurer, harvester, groundskeeper, cartographer, sentry, tinker):
    bots.set_status(bot, BotStatus.ACTIVE, now=NOW, wake=first_wake(bot.wake, now=NOW))

# prospector stays DRAFT, so the roster has something inactive.

# ── budgets ───────────────────────────────────────────────────────
budgets.grant(scout.id, 500)
budgets.grant(librarian.id, 200)
budgets.grant(treasurer.id, 50)
for index in range(37):
    budgets.charge(scout.id, run_id=f"seed{index}", now=NOW - 3600 + index)

# ── workspaces and a report ───────────────────────────────────────
# Every bot, not just two: the dock's Files tab reads the real directory, so a
# fleet where seven of nine have nothing on disk demonstrates the empty state
# rather than the feature.
for bot in (scout, librarian, treasurer, harvester, groundskeeper, cartographer, sentry, tinker):
    workspace.prepare(bot, now=NOW)
workspace.write_report(
    scout,
    "Week 34 — recall recovered",
    "Recall on the eval set moved 0.71 to 0.78 after the tokeniser fix.\n\n"
    "One flaky test remains at index 14; it fails about one run in nine.",
    run_id="seed-run",
    now=NOW - 7200,
)

# ── driving the fleet ─────────────────────────────────────────────
# Each bot reaches its state in isolation: a live run outranks any schedule, so
# a tick taken while everyone is active gives whichever bot it happened to pick
# a run, and masks every state below it.
fleet = BotSupervisor(
    store,
    Offline(),
    bots,
    WorkloadRegistry(),
    messages=messages,
    approvals=approvals,
    budgets=budgets,
)
EVERYONE = (
    "scout",
    "librarian",
    "treasurer",
    "harvester",
    "groundskeeper",
    "cartographer",
    "sentry",
    "tinker",
)


def only(slug: str) -> None:
    """Leave one bot active and hold the rest."""
    for name in EVERYONE:
        bot = bots.by_slug(name)
        want = BotStatus.ACTIVE if name == slug else BotStatus.PAUSED
        if bot.status is not want:
            bots.set_status(
                bot, want, now=NOW, reason=None if want is BotStatus.ACTIVE else "held"
            )


def settle(clock: int) -> int:
    """Drive whichever bot is active from due, through a question, to an answer."""
    for offset in range(4):
        fleet.fleet_tick(now=clock + offset)
    parked = next((r for r in store.list_runs() if r.state is RunState.WAITING_HUMAN), None)
    if parked is None:
        return clock + 60
    request = approvals.open_for_run(parked.id)
    approvals.decide(
        request,
        approved=True,
        decided_by="human:dpal",
        now=clock + 5,
        choice="continue",
        run_version=parked.version,
    )
    fleet.fleet_tick(now=clock + 6)
    return clock + 1800


# scout gets a history, so its ledger is not an empty promise.
# Forward from NOW, never behind it: a bot's first wake is its activation, so
# ticking at an earlier clock finds nothing due and leaves the ledger empty.
only("scout")
clock = NOW
for _ in range(3):
    clock = settle(clock)

# harvester is left mid-question, which is what the operator sees first.
only("harvester")
for offset in range(4):
    fleet.fleet_tick(now=clock + offset)

# Everyone back, then into their own states.
for name in EVERYONE:
    bot = bots.by_slug(name)
    if bot.status is BotStatus.PAUSED:
        bots.set_status(bot, BotStatus.ACTIVE, now=NOW)

# ── the other states ──────────────────────────────────────────────
bots.block_vendor("claude-native", NOW + 900, "usage limit reached")

backing = bots.by_slug("groundskeeper")
bots.record_wake(backing, Wake(NOW + 480, backing.wake_reason, 3, 0), RunOutcome.NO_WORK, now=NOW)

stranded = bots.by_slug("librarian")
bots.record_wake(stranded, Wake(None, stranded.wake_reason, 0, 0), None, now=NOW)

running = bots.by_slug("cartographer")
run = store.create_run(Run.new("heartbeat", {"beat": 1}, now=NOW, bot_id=running.id))
# A session id, so the page can offer "Open session". A real workload records
# this itself when it opens a body; the heartbeat used here never opens one, so
# the demo would otherwise have no live harness to point at — which is the one
# thing the roster exists to get you to.
store.transition(run, RunState.DISPATCHING, now=NOW, artifacts={"sessions": ["conv_demo"]})

# ── a verb only the owner may answer ──────────────────────────────
# treasurer parks on `spend`, which is in ALWAYS_OWNER. Nothing in its own
# channel can resolve this, and the roster shows it under its own heading —
# the discontinuity between the two surfaces is the capability boundary.
spend_run = store.create_run(
    Run.new("heartbeat", {"beat": 1}, now=NOW - 1080, bot_id=treasurer.id)
)
for step in (
    RunState.DISPATCHING,
    RunState.COLLECTING,
    RunState.EVALUATING,
    RunState.WAITING_HUMAN,
):
    spend_run = store.transition(spend_run, step, now=NOW - 1080)
approvals.request(
    bot_id=treasurer.id,
    run_id=spend_run.id,
    run_version=spend_run.version,
    verb="spend",
    parameters={"provider": "polygon", "usd": 40.00},
    question="Market data top-up",
    # The store insists on at least one, and the card does not render them as
    # buttons: a signature is not a menu, so the only choices are sign and
    # refuse, and neither comes from a list a bot authored.
    options=["pay"],
    evidence={"amount": "$40.00", "provider": "polygon", "runway": "6 days left"},
    now=NOW - 1080,
)

# ── a bot proposing a bot ─────────────────────────────────────────
spawns.propose(
    scout,
    {
        "slug": "prospector-child",
        "persona": "You are speculative and you say so.",
        "mission": "Find datasets worth indexing, and hand them to librarian.",
        "workload": HEARTBEAT,
        "wake": {"kind": "continuous", "precondition": "always", "min_interval_s": 600},
    },
    rationale="I keep finding datasets I have no time to evaluate. One helper, 40 iterations.",
    now=NOW - 1800,
    allowance=40,
)

# ── some channel traffic ──────────────────────────────────────────
messages.post(
    sentry.id,
    scout.address,
    MessageKind.BOT_TO_BOT,
    "Have you indexed the 2026-W34 batch? I need it before the next eval.",
    now=NOW - 600,
    deliver_to=[sentry.address],
)
remember(scout, messages, "the eval set has a flaky test at index 14", now=NOW - 3600)
messages.post(
    scout.id,
    "system",
    MessageKind.REPORT,
    "Week 34 — recall recovered: 0.71 to 0.78 after the tokeniser fix.",
    now=NOW - 7200,
)

print(f"seeded {len(bots.list())} bots at {DB}")
for entry in bots.list():
    print(f"  {entry.slug:<16}{entry.status.value}")
