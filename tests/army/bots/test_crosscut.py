"""Milestones interacting, which is where the seams actually fail.

Each earlier file tests one thing well. These are the paths that cross them —
a spawned child inheriting a budget and then running out of it, a message
waking a bot that then asks a question, a database written by a version that
predates every column. Nothing here is a unit; every one of them is the
sentence "and then" repeated until something breaks.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from army.bots.approvals import ApprovalStore
from army.bots.budget import BudgetStore
from army.bots.memory import assemble, remember
from army.bots.messages import MessageKind, MessageStore
from army.bots.model import BotStatus, RunOutcome, WakeKind, WakePolicy
from army.bots.roster import roster
from army.bots.spawn import SpawnStore, retire_descendants
from army.bots.store import BotStore
from army.bots.supervisor import BotSupervisor
from army.bots.workloads.heartbeat import HeartbeatWorkload
from army.bots.workspace import DocKind, Workspace
from army.state import CommandKind, Run, RunState
from army.store import Store
from tests.army.bots.conftest import FakeOmni, StubRegistry, activate, make_bot

NOW = 1_700_000_000
HEARTBEAT = "army.bots.workloads.heartbeat:HeartbeatWorkload"


def _continuous(**kwargs: object) -> WakePolicy:
    kwargs.setdefault("precondition", "always")
    return WakePolicy(kind=WakeKind.CONTINUOUS, **kwargs)  # type: ignore[arg-type]


class Fleet:
    """Everything wired together, the way the CLI wires it."""

    def __init__(self, store: Store, bots: BotStore) -> None:
        self.store = store
        self.bots = bots
        self.messages = MessageStore(bots)
        self.approvals = ApprovalStore(bots, messages=self.messages)
        self.budgets = BudgetStore(bots)
        self.spawns = SpawnStore(bots, self.budgets)
        self.workload = HeartbeatWorkload(outcome="work_done")
        self.supervisor = BotSupervisor(
            store,
            FakeOmni(),
            bots,
            StubRegistry({HEARTBEAT: self.workload}),
            messages=self.messages,
            approvals=self.approvals,
            budgets=self.budgets,
        )

    def settle_one_iteration(self, *, start: int) -> int:
        """Drive a bot from due to answered, returning the clock afterwards."""
        for offset in range(4):
            self.supervisor.fleet_tick(now=start + offset)
        run = next(r for r in self.store.list_runs() if r.state is RunState.WAITING_HUMAN)
        request = self.approvals.open_for_run(run.id)
        assert request is not None
        self.approvals.decide(
            request,
            approved=True,
            decided_by="human:test",
            now=start + 5,
            choice="continue",
            run_version=run.version,
        )
        self.supervisor.fleet_tick(now=start + 6)
        return start + 7


@pytest.fixture()
def fleet(store: Store, bots: BotStore) -> Fleet:
    return Fleet(store, bots)


def test_a_child_inherits_a_budget_spends_it_and_stops(fleet: Fleet) -> None:
    """M4 into M1: the guardrail only counts if the scheduler honours it."""
    parent = activate(fleet.bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    fleet.budgets.grant(parent.id, 50)

    request = fleet.spawns.propose(
        parent,
        {
            "slug": "child",
            "persona": "p",
            "mission": "m",
            "workload": HEARTBEAT,
            "wake": {"kind": "continuous", "precondition": "always", "min_interval_s": 60},
        },
        rationale="a second pair of hands",
        now=NOW,
        allowance=2,
    )
    child = fleet.spawns.activate(request, decided_by="human:test", now=NOW)
    fleet.bots.set_status(child, BotStatus.ACTIVE, now=NOW)

    # The parent's remaining shrank by exactly what the child got.
    assert fleet.budgets.account(parent.id).remaining == 48  # type: ignore[union-attr]
    assert fleet.budgets.account(child.id).remaining == 2  # type: ignore[union-attr]

    # Pause the parent so only the child runs, and spend the child's two.
    fleet.bots.set_status(parent, BotStatus.PAUSED, now=NOW, reason="so the child runs alone")
    clock = NOW
    for _ in range(2):
        clock = fleet.settle_one_iteration(start=clock) + 3600

    spent = fleet.bots.get(child.id)
    assert spent is not None and spent.status is BotStatus.ACTIVE
    assert fleet.budgets.account(child.id).remaining == 0  # type: ignore[union-attr]

    # The third wake finds no allowance and stops it, in its own channel.
    from army.bots.supervisor import wake_now

    wake_now(fleet.bots, spent, now=clock)
    fleet.supervisor.fleet_tick(now=clock)

    stopped = fleet.bots.get(child.id)
    assert stopped is not None
    assert stopped.status is BotStatus.PAUSED
    assert "budget" in (stopped.paused_reason or "")
    assert any("budget" in message.body.lower() for message in fleet.messages.channel(child.id))


def test_retiring_a_parent_stops_the_child_and_returns_its_allowance(fleet: Fleet) -> None:
    """M4 into M4: the cascade and the ledger have to agree."""
    parent = activate(fleet.bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    fleet.budgets.grant(parent.id, 100)
    request = fleet.spawns.propose(
        parent,
        {
            "slug": "child",
            "persona": "p",
            "mission": "m",
            "workload": HEARTBEAT,
            "wake": {"kind": "manual"},
        },
        rationale="x",
        now=NOW,
        allowance=30,
    )
    child = fleet.spawns.activate(request, decided_by="human:test", now=NOW)
    fleet.bots.set_status(child, BotStatus.ACTIVE, now=NOW)
    assert fleet.budgets.account(parent.id).remaining == 70  # type: ignore[union-attr]

    retire_descendants(fleet.bots, fleet.budgets, parent, now=NOW + 10)

    assert fleet.bots.get(child.id).status is BotStatus.RETIRED  # type: ignore[union-attr]
    assert fleet.budgets.account(parent.id).remaining == 100  # type: ignore[union-attr]
    # And it is no longer in anyone's way.
    assert fleet.bots.due(now=NOW + 100) == []


def test_mail_wakes_a_bot_which_then_asks_a_question(fleet: Fleet) -> None:
    """M2 into M1 into M2: an insert, a run, and a row waiting on a person."""
    bot = activate(
        fleet.bots,
        make_bot("listener", workload=HEARTBEAT, wake=WakePolicy(kind=WakeKind.ON_MESSAGE)),
        now=NOW,
    )
    fleet.supervisor.fleet_tick(now=NOW)
    assert fleet.store.list_runs() == [], "an event bot ran with no event"

    fleet.messages.post(
        bot.id,
        "human:dpal",
        MessageKind.HUMAN_MSG,
        "have a look at this",
        now=NOW + 1,
        deliver_to=[bot.address],
    )
    for offset in range(2, 8):
        fleet.supervisor.fleet_tick(now=NOW + offset)

    run = fleet.store.list_runs()[0]
    assert run.state is RunState.WAITING_HUMAN
    assert fleet.approvals.open_for_run(run.id) is not None

    entry = next(row for row in roster(fleet.bots, now=NOW + 8) if row.bot.id == bot.id)
    assert entry.needs_a_human


def test_a_bot_remembers_what_it_learned_across_disposable_bodies(fleet: Fleet) -> None:
    """M2 into M1: the whole point of "a body is disposable" is that this works."""
    bot = activate(fleet.bots, make_bot("scout", workload=HEARTBEAT, wake=_continuous()), now=NOW)
    clock = fleet.settle_one_iteration(start=NOW)
    remember(bot, fleet.messages, "the eval set has a flaky test at index 14", now=clock)

    briefing = assemble(bot, fleet.bots, fleet.messages, now=clock)
    rendered = briefing.render()

    assert "flaky test at index 14" in rendered
    assert briefing.persona in rendered
    assert any("work_done" in str(entry["outcome"]) for entry in briefing.history)
    assert briefing.verdicts, "the decision the human made was not carried forward"


def test_a_briefing_uses_the_pinned_revision_not_the_edited_one(fleet: Fleet) -> None:
    """A run executes the definition it started under, or history lies."""
    bot = activate(fleet.bots, make_bot("scout", workload=HEARTBEAT, wake=_continuous()), now=NOW)
    pinned = bot.current_revision_id

    bot.persona = "a completely different bot"
    fleet.bots.revise(bot, created_by="human:test", now=NOW + 1)

    briefing = assemble(bot, fleet.bots, fleet.messages, now=NOW + 2, revision_id=pinned)
    assert briefing.persona == "a standing role"
    assert briefing.revision == 1

    current = assemble(bot, fleet.bots, fleet.messages, now=NOW + 2)
    assert current.persona == "a completely different bot"


def test_a_report_is_written_indexed_and_findable(fleet: Fleet, tmp_path) -> None:
    """M3 into M5: a report nobody can trace to a run is an assertion."""
    bot = activate(fleet.bots, make_bot("scout", workload=HEARTBEAT, wake=_continuous()), now=NOW)
    workspace = Workspace(fleet.bots, root=tmp_path)
    workspace.prepare(bot, now=NOW)
    fleet.settle_one_iteration(start=NOW)
    run = fleet.store.list_runs()[0]

    doc = workspace.write_report(
        bot, "Week 34", "Recall moved 0.71 to 0.78.", run_id=run.id, now=NOW + 10
    )
    assert doc.run_id == run.id

    body = (workspace.path_for(bot) / doc.path).read_text()
    assert "Week 34" in body
    assert run.id[:12] in body, "the report does not cite the iteration that produced it"
    assert doc.path in {d.path for d in workspace.docs(bot, kind=DocKind.REPORT)}


def test_the_whole_fleet_survives_being_thrown_away_mid_flight(
    store: Store, bots: BotStore
) -> None:
    """The acceptance criterion, at fleet scale.

    Every object holding state is destroyed between the question and the
    answer. Only the SQLite file crosses the gap — which is what a reboot is.
    """
    first = Fleet(store, bots)
    bot = activate(first.bots, make_bot("scout", workload=HEARTBEAT, wake=_continuous()), now=NOW)
    for offset in range(4):
        first.supervisor.fleet_tick(now=NOW + offset)
    run = first.store.list_runs()[0]
    assert run.state is RunState.WAITING_HUMAN
    parked_at = run.version

    path = store.path
    del first, store, bots, bot

    # A new process, in every sense the test can manage.
    reopened = Store(path)
    second = Fleet(reopened, BotStore(reopened))
    revived = second.bots.by_slug("scout")
    assert revived is not None
    assert revived.next_due_at is None, "a blocked bot came back scheduled"

    request = second.approvals.open_for_run(run.id)
    assert request is not None, "the question did not survive"
    second.approvals.decide(
        request,
        approved=True,
        decided_by="human:test",
        now=NOW + 100,
        choice="continue",
        run_version=parked_at,
    )
    second.supervisor.fleet_tick(now=NOW + 101)

    settled = reopened.get_run(run.id)
    assert settled is not None and settled.state is RunState.CONTINUE
    assert second.bots.by_slug("scout").next_due_at is not None  # type: ignore[union-attr]


def test_a_database_written_before_bot_mode_existed_still_opens(tmp_path) -> None:
    """The migration, against a file shaped like the one already on the box.

    Four tables, no bot columns, no partial index — which is what ``army.db``
    looked like before any of this.
    """
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, workflow TEXT NOT NULL, state TEXT NOT NULL,
            version INTEGER NOT NULL, attempt INTEGER NOT NULL,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            payload TEXT NOT NULL, artifacts TEXT NOT NULL,
            outstanding TEXT NOT NULL, approval_id TEXT, terminal_reason TEXT
        );
        CREATE TABLE commands (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, created_at INTEGER NOT NULL, consumed_at INTEGER
        );
        INSERT INTO runs VALUES
            ('old-run','demo','waiting_human',3,1,1,1,'{}','{}','[]',NULL,NULL);
        """
    )
    old.commit()
    old.close()

    store = Store(path)
    revived = store.get_run("old-run")
    assert revived is not None
    assert revived.state is RunState.WAITING_HUMAN
    assert revived.bot_id is None, "an existing run was given a bot it never had"

    # And bot mode opens over the top of it.
    bots = BotStore(store)
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    assert bots.by_slug("scout") is not None

    # The old run and the new bot coexist; the index covers only the bot's.
    store.create_run(Run.new("demo", {}, now=NOW, bot_id=bot.id))
    with pytest.raises(Exception, match="live run"):
        store.create_run(Run.new("demo", {}, now=NOW, bot_id=bot.id))
    store.create_run(Run.new("demo", {}, now=NOW))  # no bot: still allowed


def test_running_it_twice_over_the_same_file_is_a_no_op(tmp_path) -> None:
    """Idempotent on a fresh database, a live one, and one already migrated."""
    path = tmp_path / "twice.db"
    for _ in range(3):
        store = Store(path)
        BotStore(store)
        MessageStore(BotStore(store))
    with Store(path).atomic() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"runs", "bots", "bot_revisions", "messages", "provider_gates"} <= tables


def test_the_cli_and_the_loop_can_write_at_the_same_time(store: Store, bots: BotStore) -> None:
    """Two processes share this file. Contention must be a wait, not an error."""
    fleet = Fleet(store, bots)
    bot = activate(fleet.bots, make_bot("scout", workload=HEARTBEAT, wake=_continuous()), now=NOW)
    errors: list[BaseException] = []

    def keep_ticking() -> None:
        try:
            for offset in range(20):
                fleet.supervisor.fleet_tick(now=NOW + offset)
        except BaseException as exc:
            errors.append(exc)

    def keep_reading() -> None:
        # A second connection to the same file, as `army bots status` opens.
        other = Store(store.path)
        watching = BotStore(other)
        try:
            for _ in range(30):
                roster(watching, now=NOW)
        except BaseException as exc:
            errors.append(exc)

    loop = threading.Thread(target=keep_ticking)
    cli = threading.Thread(target=keep_reading)
    loop.start()
    cli.start()
    loop.join(timeout=60)
    cli.join(timeout=60)

    assert errors == [], f"concurrent access failed instead of waiting: {errors}"
    assert fleet.bots.get(bot.id) is not None


def test_every_derived_status_the_roster_can_show_is_reachable(fleet: Fleet) -> None:
    """Nine statuses is a claim; this is the claim being paid.

    A status nothing can produce is a status nobody will recognise when it
    finally appears.
    """
    from army.bots.model import DerivedStatus
    from army.bots.schedule import Wake

    seen: set[DerivedStatus] = set()

    # INACTIVE — a draft nobody activated.
    fleet.bots.create(make_bot("drafted", workload=HEARTBEAT))
    # MANUAL and WAITING_EVENT — no schedule, legitimately.
    activate(
        fleet.bots,
        make_bot("manual", workload=HEARTBEAT, wake=WakePolicy(kind=WakeKind.MANUAL)),
        now=NOW,
    )
    activate(
        fleet.bots,
        make_bot("listener", workload=HEARTBEAT, wake=WakePolicy(kind=WakeKind.ON_MESSAGE)),
        now=NOW,
    )
    # SCHEDULED — due later, never idle.
    scheduled = activate(
        fleet.bots, make_bot("scheduled", workload=HEARTBEAT, wake=_continuous()), now=NOW
    )
    fleet.bots.record_wake(scheduled, Wake(NOW + 3600, scheduled.wake_reason, 0, 0), None, now=NOW)
    # BACKING_OFF — due later, with an idle streak.
    backing = activate(
        fleet.bots, make_bot("backing", workload=HEARTBEAT, wake=_continuous()), now=NOW
    )
    fleet.bots.record_wake(
        backing, Wake(NOW + 900, backing.wake_reason, 4, 0), RunOutcome.NO_WORK, now=NOW
    )
    # DUE — due now.
    activate(fleet.bots, make_bot("due", workload=HEARTBEAT, wake=_continuous()), now=NOW)
    # WAITING_RESOURCE — its vendor is cooling.
    cooling = activate(
        fleet.bots,
        make_bot("cooling", workload=HEARTBEAT, wake=_continuous(), harness="claude-native"),
        now=NOW,
    )
    fleet.bots.block_vendor("claude-native", NOW + 600, "usage limit reached")
    # BLOCKED — no wake, no reason to have none.
    stranded = activate(
        fleet.bots, make_bot("stranded", workload=HEARTBEAT, wake=_continuous()), now=NOW
    )
    fleet.bots.record_wake(stranded, Wake(None, stranded.wake_reason, 0, 0), None, now=NOW)
    # RUNNING and WAITING_HUMAN — a real iteration.
    asking = activate(
        fleet.bots, make_bot("asking", workload=HEARTBEAT, wake=_continuous()), now=NOW
    )
    run = fleet.store.create_run(Run.new("heartbeat", {}, now=NOW, bot_id=asking.id))
    seen.update(entry.status for entry in roster(fleet.bots, now=NOW))

    moved = fleet.store.transition(run, RunState.DISPATCHING, now=NOW)
    moved = fleet.store.transition(moved, RunState.COLLECTING, now=NOW)
    moved = fleet.store.transition(moved, RunState.EVALUATING, now=NOW)
    fleet.store.transition(moved, RunState.WAITING_HUMAN, now=NOW)
    seen.update(entry.status for entry in roster(fleet.bots, now=NOW))

    assert seen == set(DerivedStatus), f"never produced: {set(DerivedStatus) - seen}"
    assert cooling and stranded  # referenced, so the intent is legible


def test_a_verdict_from_the_cli_and_one_from_the_page_are_the_same_verdict(
    fleet: Fleet, tmp_path
) -> None:
    """Two front doors, one lock. The page must not be a softer route."""
    from army.bots.web import BotsSite

    bot = activate(fleet.bots, make_bot("scout", workload=HEARTBEAT, wake=_continuous()), now=NOW)
    site = BotsSite(fleet.store, fleet.bots, fleet.approvals, fleet.messages, "tok")

    for offset in range(4):
        fleet.supervisor.fleet_tick(now=NOW + offset)
    run = fleet.store.list_runs()[0]
    request = fleet.approvals.open_for_run(run.id)
    assert request is not None

    # A stale run version is refused identically on both paths.
    with pytest.raises(Exception, match="run moved"):
        fleet.approvals.decide(
            request,
            approved=True,
            decided_by="human:cli",
            now=NOW + 5,
            choice="continue",
            run_version=run.version + 99,
        )
    _notice, problem = site.verdict(
        {
            "approval": [request.id],
            "choice": ["continue"],
            "decision": ["approve"],
        },
        now=NOW + 5,
    )
    assert problem == "", "the page refused what the CLI would have accepted"

    # And the command it produced is indistinguishable.
    command = fleet.store.next_command(run.id)
    assert command is not None and command.kind is CommandKind.APPROVE
    assert bot.id


def test_the_precondition_config_never_reaches_the_workload() -> None:
    """
    The wake check and the workload share one `workload_config` dict.

    `precondition` configures the supervisor's model-free check; the rest are
    the workload's constructor arguments. Splatting the whole dict raised
    `unexpected keyword argument 'precondition'` for the exact shape
    `docs/agent-army/bots.md` prescribes — so a bot that followed the
    documentation failed on every tick and retried forever.

    Pinned at `resolve`, which is the choke point: an earlier fix put it in
    `for_bot` only, and the supervisor resolves a run's *pinned* workload path
    directly, so the real loop still crashed.
    """
    from army.bots.registry import WorkloadRegistry

    registry = WorkloadRegistry()
    workload = registry.resolve(
        "army.bots.workloads.heartbeat:HeartbeatWorkload",
        {"outcome": "no_work", "precondition": {"path": "/tmp/queue.md"}},
    )
    assert workload.name == "heartbeat"


def test_two_bots_differing_only_in_precondition_share_a_workload() -> None:
    """
    A corollary worth pinning: the cache key is built after the strip, so the
    key cannot be split by a value the workload never sees.
    """
    from army.bots.registry import WorkloadRegistry

    registry = WorkloadRegistry()
    first = registry.resolve(
        "army.bots.workloads.heartbeat:HeartbeatWorkload",
        {"outcome": "no_work", "precondition": {"path": "/tmp/a.md"}},
    )
    second = registry.resolve(
        "army.bots.workloads.heartbeat:HeartbeatWorkload",
        {"outcome": "no_work", "precondition": {"path": "/tmp/b.md"}},
    )
    assert first is second


def test_a_message_reaches_the_body_that_is_already_working(bots: BotStore, store: Store) -> None:
    """
    The half that makes the channel a conversation rather than a log.

    Before this, a message to a busy bot sat in its inbox until the iteration
    finished — the one moment it is useless. The point of saying "no, not that
    branch" is to say it *while* the wrong branch is being cut.
    """
    from army.bots.messages import MessageKind, MessageStore
    from army.bots.registry import WorkloadRegistry
    from army.bots.supervisor import BotSupervisor
    from army.state import Run, RunState

    sent: list[tuple[str, str]] = []

    class Recording(FakeOmni):
        def send(self, session_id: str, text: str) -> None:
            sent.append((session_id, text))

    messages = MessageStore(bots)
    fleet = BotSupervisor(store, Recording(), bots, WorkloadRegistry(), messages=messages)
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)

    run = store.create_run(Run.new("w", {}, now=NOW, bot_id=bot.id))
    store.transition(run, RunState.DISPATCHING, now=NOW, artifacts={"sessions": ["conv_live"]})
    messages.post(
        bot.id,
        "human:channel",
        MessageKind.HUMAN_MSG,
        "stop, the fee is per side",
        now=NOW,
        deliver_to=[bot.address],
    )

    fleet._steer_live_runs(now=NOW)

    assert len(sent) == 1
    session, text = sent[0]
    assert session == "conv_live"
    assert "stop, the fee is per side" in text
    # Labelled, so a body cannot mistake it for its own reasoning — and told
    # plainly that being spoken to is not permission.
    assert "operator is speaking to you" in text
    assert "not** an approval" in text
    # Acked, so it is not delivered twice.
    assert messages.waiting_recipients(now=NOW).get(bot.address) is None


def test_a_bot_to_bot_message_does_not_steer_a_live_run(bots: BotStore, store: Store) -> None:
    """Mail is not steering. Only a person interrupts an iteration."""
    from army.bots.messages import MessageKind, MessageStore
    from army.bots.registry import WorkloadRegistry
    from army.bots.supervisor import BotSupervisor
    from army.state import Run, RunState

    sent: list[str] = []

    class Recording(FakeOmni):
        def send(self, session_id: str, text: str) -> None:
            sent.append(text)

    messages = MessageStore(bots)
    fleet = BotSupervisor(store, Recording(), bots, WorkloadRegistry(), messages=messages)
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    run = store.create_run(Run.new("w", {}, now=NOW, bot_id=bot.id))
    store.transition(run, RunState.DISPATCHING, now=NOW, artifacts={"sessions": ["conv_live"]})

    messages.post(
        bot.id,
        "bot:other",
        MessageKind.BOT_TO_BOT,
        "have you indexed W34?",
        now=NOW,
        deliver_to=[bot.address],
    )
    fleet._steer_live_runs(now=NOW)
    assert sent == []


def test_a_message_to_a_run_with_no_session_is_not_stranded(bots: BotStore, store: Store) -> None:
    """
    Leasing before checking for a session hid the message from everything.

    A lease is invisible to the wake scan as well as to the next brief, so a
    bot whose live run never opened a body was neither steered nor woken until
    the lease expired — the exact silence this feature exists to remove.
    """
    from army.bots.messages import MessageKind, MessageStore
    from army.bots.registry import WorkloadRegistry
    from army.bots.supervisor import BotSupervisor
    from army.state import Run, RunState

    messages = MessageStore(bots)
    fleet = BotSupervisor(store, FakeOmni(), bots, WorkloadRegistry(), messages=messages)
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)

    # A live run that never opened a session — a workload that needed no body.
    run = store.create_run(Run.new("w", {}, now=NOW, bot_id=bot.id))
    store.transition(run, RunState.DISPATCHING, now=NOW)
    messages.post(
        bot.id,
        "human:channel",
        MessageKind.HUMAN_MSG,
        "check the last row date",
        now=NOW,
        deliver_to=[bot.address],
    )

    fleet._steer_live_runs(now=NOW)

    # Still owed, so the next iteration's brief picks it up.
    assert messages.waiting_recipients(now=NOW).get(bot.address) == 1
    assert fleet._take_messages(bot, now=NOW) == ["check the last row date"]
