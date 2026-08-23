"""Bots that make bots, and the arithmetic that stops them.

The claim to prove is narrow and absolute: **a bot cannot create capacity by
creating bots.** Every other guardrail here — depth, fan-out, the fleet cap,
the TTL, the cascade — is a fence. The budget is the floor.
"""

from __future__ import annotations

import pytest

from army.bots.budget import BudgetExhausted, BudgetStore, Window
from army.bots.model import MAX_ACTIVE_BOTS, MAX_DEPTH, MAX_FANOUT, BotStatus
from army.bots.spawn import (
    SpawnRefused,
    SpawnState,
    SpawnStore,
    retire_descendants,
)
from army.bots.store import BotStore
from tests.army.bots.conftest import activate, make_bot

NOW = 1_700_000_000

HEARTBEAT = "army.bots.workloads.heartbeat:HeartbeatWorkload"


def _spec(slug: str, **extra: object) -> dict:
    spec = {
        "slug": slug,
        "persona": "a child",
        "mission": "help the parent",
        "workload": HEARTBEAT,
        "wake": {"kind": "manual"},
    }
    spec.update(extra)  # type: ignore[arg-type]
    return spec


@pytest.fixture()
def budgets(bots: BotStore) -> BudgetStore:
    return BudgetStore(bots)


@pytest.fixture()
def spawns(bots: BotStore, budgets: BudgetStore) -> SpawnStore:
    return SpawnStore(bots, budgets)


# ── the floor ─────────────────────────────────────────────────────


def test_a_child_is_funded_out_of_its_parent_not_in_addition_to_it(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    """The whole anti-replication argument, in one assertion."""
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    budgets.grant(parent.id, 100)

    request = spawns.propose(parent, _spec("child"), rationale="needs help", now=NOW, allowance=30)
    child = spawns.activate(request, decided_by="human:dpal", now=NOW)

    parent_account = budgets.account(parent.id)
    child_account = budgets.account(child.id)
    assert parent_account is not None and child_account is not None
    assert child_account.allowance == 30
    assert parent_account.remaining == 70
    # The fleet's total capacity did not change.
    assert parent_account.remaining + child_account.remaining == 100


def test_a_parent_cannot_fund_a_child_it_cannot_afford(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    budgets.grant(parent.id, 10)
    request = spawns.propose(parent, _spec("child"), rationale="x", now=NOW, allowance=30)
    with pytest.raises(BudgetExhausted, match="10 iterations left"):
        spawns.activate(request, decided_by="human:dpal", now=NOW)
    assert bots.by_slug("child") is None


def test_an_unbudgeted_parent_may_not_fund_anything(bots: BotStore, spawns: SpawnStore) -> None:
    """Otherwise "carve from the parent" is a no-op and the fleet is unbounded."""
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    request = spawns.propose(parent, _spec("child"), rationale="x", now=NOW, allowance=5)
    with pytest.raises(BudgetExhausted, match="no budget to carve from"):
        spawns.activate(request, decided_by="human:dpal", now=NOW)


def test_two_children_racing_the_same_capacity_cannot_both_win(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    """Concurrent approvals reading the same remaining would overcommit it."""
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    budgets.grant(parent.id, 40)
    first = spawns.propose(parent, _spec("one"), rationale="x", now=NOW, allowance=30)
    second = spawns.propose(parent, _spec("two"), rationale="x", now=NOW, allowance=30)

    spawns.activate(first, decided_by="human:dpal", now=NOW)
    with pytest.raises(BudgetExhausted):
        spawns.activate(second, decided_by="human:dpal", now=NOW)


def test_a_poison_mission_is_stopped_by_the_budget_and_nothing_else(
    bots: BotStore, budgets: BudgetStore
) -> None:
    """A bot that always reports work done has no idle streak to back it off."""
    bot = activate(bots, make_bot("greedy", workload=HEARTBEAT), now=NOW)
    budgets.grant(bot.id, 3)
    for index in range(3):
        budgets.charge(bot.id, run_id=f"run{index}", now=NOW)
    with pytest.raises(BudgetExhausted, match="0 of 3"):
        budgets.charge(bot.id, run_id="run3", now=NOW)


def test_usage_is_recorded_even_with_no_allowance_configured(
    bots: BotStore, budgets: BudgetStore
) -> None:
    """History cannot be backfilled, and it is what a real budget is set from."""
    bot = activate(bots, make_bot("unbudgeted", workload=HEARTBEAT), now=NOW)
    budgets.charge(bot.id, run_id="run1", now=NOW)
    budgets.charge(bot.id, run_id="run2", now=NOW + 1)
    assert len(budgets.usage(bot.id)) == 2


def test_a_retired_child_returns_its_unused_allowance(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    """A fleet that churns children would otherwise leak capacity permanently."""
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    budgets.grant(parent.id, 100)
    request = spawns.propose(parent, _spec("child"), rationale="x", now=NOW, allowance=40)
    spawns.activate(request, decided_by="human:dpal", now=NOW)
    assert budgets.account(parent.id).remaining == 60  # type: ignore[union-attr]

    retire_descendants(bots, budgets, parent, now=NOW + 10)
    assert budgets.account(parent.id).remaining == 100  # type: ignore[union-attr]


# ── the fences ────────────────────────────────────────────────────


def test_a_child_may_not_create_grandchildren(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    budgets.grant(parent.id, 100)
    request = spawns.propose(parent, _spec("child"), rationale="x", now=NOW, allowance=50)
    child = spawns.activate(request, decided_by="human:dpal", now=NOW)
    assert child.depth == 1

    bots.set_status(child, BotStatus.ACTIVE, now=NOW)
    budgets.grant(child.id, 50)
    grandchild = activate(bots, make_bot("mid", workload=HEARTBEAT), now=NOW)
    grandchild.depth = MAX_DEPTH
    with pytest.raises(SpawnRefused, match="grandchild"):
        spawns.propose(grandchild, _spec("too-deep"), rationale="x", now=NOW)


def test_one_parent_cannot_flood_the_roster_by_breadth(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    budgets.grant(parent.id, 1000)
    for index in range(MAX_FANOUT):
        request = spawns.propose(
            parent, _spec(f"child-{index}"), rationale="x", now=NOW, allowance=10
        )
        spawns.activate(request, decided_by="human:dpal", now=NOW)
    with pytest.raises(SpawnRefused, match="cap is 3"):
        spawns.propose(parent, _spec("one-too-many"), rationale="x", now=NOW)


def test_the_fleet_cap_holds_across_unrelated_parents(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    """Ten active bots is what the box was sized for, however they got there."""
    for index in range(MAX_ACTIVE_BOTS):
        activate(bots, make_bot(f"bot-{index}", workload=HEARTBEAT), now=NOW)
    parent = bots.by_slug("bot-0")
    assert parent is not None
    budgets.grant(parent.id, 100)
    with pytest.raises(SpawnRefused, match="cap is 10"):
        spawns.propose(parent, _spec("eleven"), rationale="x", now=NOW)


def test_retiring_a_parent_retires_everything_under_it(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    """A retired parent whose children run on is a fleet nobody is watching."""
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    budgets.grant(parent.id, 200)
    for index in range(2):
        request = spawns.propose(
            parent, _spec(f"child-{index}"), rationale="x", now=NOW, allowance=10
        )
        child = spawns.activate(request, decided_by="human:dpal", now=NOW)
        bots.set_status(child, BotStatus.ACTIVE, now=NOW)

    retired = retire_descendants(bots, budgets, parent, now=NOW + 1)
    assert {bot.slug for bot in retired} == {"child-0", "child-1"}
    assert all(bots.by_slug(slug).status is BotStatus.RETIRED for slug in ("child-0", "child-1"))  # type: ignore[union-attr]


# ── propose, don't activate ───────────────────────────────────────


def test_a_proposal_creates_no_bot_until_a_human_says_so(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    """The rule that makes runaway replication structurally impossible."""
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    budgets.grant(parent.id, 100)
    spawns.propose(parent, _spec("child"), rationale="I need a researcher", now=NOW)

    assert bots.by_slug("child") is None
    assert [r.slug for r in spawns.pending()] == ["child"]


def test_an_activated_child_starts_in_draft_not_running(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    """Approving the proposal says it should exist, not that it should start.

    A person approving ten proposals in a row has not started ten bots.
    """
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    budgets.grant(parent.id, 100)
    request = spawns.propose(parent, _spec("child"), rationale="x", now=NOW, allowance=10)
    child = spawns.activate(request, decided_by="human:dpal", now=NOW)
    assert child.status is BotStatus.DRAFT


def test_a_proposal_that_could_never_work_fails_at_proposal_time(
    bots: BotStore, spawns: SpawnStore
) -> None:
    """The parent deserves to know now, not after a person has read it."""
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    with pytest.raises(SpawnRefused, match="invalid definition"):
        spawns.propose(parent, _spec("child", mission=""), rationale="x", now=NOW)


def test_a_refusal_tells_the_bot_why(bots: BotStore, spawns: SpawnStore) -> None:
    """So it can stop asking, rather than proposing the same thing nightly."""
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    request = spawns.propose(parent, _spec("child"), rationale="x", now=NOW)
    spawns.refuse(request, decided_by="human:dpal", because="we do not need this", now=NOW)
    reread = spawns.get(request.id)
    assert reread is not None
    assert reread.state is SpawnState.REFUSED
    assert reread.refused_because == "we do not need this"


def test_the_same_proposal_cannot_be_activated_twice(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    budgets.grant(parent.id, 100)
    request = spawns.propose(parent, _spec("child"), rationale="x", now=NOW, allowance=10)
    spawns.activate(request, decided_by="human:dpal", now=NOW)
    with pytest.raises(SpawnRefused, match="already"):
        spawns.activate(request, decided_by="human:dpal", now=NOW)


def test_a_child_carries_its_lineage(
    bots: BotStore, budgets: BudgetStore, spawns: SpawnStore
) -> None:
    """So the fan-out cap and the cascade have something to count against."""
    parent = activate(bots, make_bot("parent", workload=HEARTBEAT), now=NOW)
    budgets.grant(parent.id, 100)
    request = spawns.propose(parent, _spec("child"), rationale="x", now=NOW, allowance=10)
    child = spawns.activate(request, decided_by="human:dpal", now=NOW)

    assert child.parent_bot_id == parent.id
    assert child.root_bot_id == parent.root_bot_id
    assert child.depth == 1
    assert child.created_by == parent.address
    assert [c.slug for c in bots.children(parent.id)] == ["child"]


def test_a_daily_window_is_separate_from_the_total(bots: BotStore, budgets: BudgetStore) -> None:
    """A bot can be within its lifetime budget and out of today's."""
    bot = activate(bots, make_bot("paced", workload=HEARTBEAT), now=NOW)
    budgets.grant(bot.id, 1000, window=Window.TOTAL)
    budgets.grant(bot.id, 2, window=Window.DAY, resets_at=NOW + 86_400)
    assert budgets.account(bot.id, window=Window.DAY).remaining == 2  # type: ignore[union-attr]
    assert budgets.account(bot.id, window=Window.TOTAL).remaining == 1000  # type: ignore[union-attr]


# ── what a bot may put in a definition ────────────────────────────
#
# A bot authors the proposed definition, and a definition is not inert: three
# of its fields are capabilities wearing the clothes of configuration. The
# human who adopts it reads a rationale the same bot wrote.


def _child_spec(**extra: object) -> dict:
    spec = {
        "slug": "helper",
        "persona": "a child",
        "mission": "help",
        "workload": "army.workloads.demo:DemoWorkload",
        "wake": {"kind": "manual"},
    }
    spec.update(extra)  # type: ignore[arg-type]
    return spec


def _configured_parent(bots: BotStore) -> object:
    parent = make_bot(
        "parent",
        workload="army.workloads.demo:DemoWorkload",
        harness="claude-native",
        workload_config={"queue_path": "/safe/queue.txt"},
    )
    return activate(bots, parent, now=NOW)


def test_a_bot_cannot_give_its_child_the_whole_filesystem(
    bots: BotStore, spawns: SpawnStore
) -> None:
    """``workspace`` becomes the sandbox's only writable path."""
    parent = _configured_parent(bots)
    with pytest.raises(SpawnRefused, match="workspace"):
        spawns.propose(parent, _child_spec(workspace="/"), rationale="x", now=NOW)


def test_a_bot_cannot_give_its_child_another_bots_cookie_jar(
    bots: BotStore, spawns: SpawnStore
) -> None:
    """A partition is a set of logged-in sessions. Naming one is taking them."""
    parent = _configured_parent(bots)
    with pytest.raises(SpawnRefused, match="browser_profile"):
        spawns.propose(
            parent,
            _child_spec(browser_profile="persist:bot-treasurer"),
            rationale="x",
            now=NOW,
        )


def test_a_bot_cannot_configure_its_child_beyond_its_own_configuration(
    bots: BotStore, spawns: SpawnStore
) -> None:
    """``workload_config`` reaches a constructor as keyword arguments.

    Unconstrained, that is an arbitrary call into operator code — a queue path
    aimed at ``~/.ssh/authorized_keys``, for instance. A bot may pass on
    configuration it was given and nothing else.
    """
    parent = _configured_parent(bots)
    with pytest.raises(SpawnRefused, match=r"does not.*itself have"):
        spawns.propose(
            parent,
            _child_spec(workload_config={"queue_path": "/x", "reviewer_agent": "z"}),
            rationale="x",
            now=NOW,
        )


def test_a_child_may_be_configured_where_its_parent_already_is(
    bots: BotStore, spawns: SpawnStore
) -> None:
    """The rule bounds escalation, not usefulness."""
    parent = _configured_parent(bots)
    request = spawns.propose(
        parent,
        _child_spec(workload_config={"queue_path": "/safe/other.txt"}),
        rationale="a second queue",
        now=NOW,
    )
    assert request.definition["workload_config"] == {"queue_path": "/safe/other.txt"}


def test_a_child_runs_on_its_parents_vendor(bots: BotStore, spawns: SpawnStore) -> None:
    """Otherwise a bot whose lane is cooling spawns its way onto a fresh one."""
    parent = _configured_parent(bots)
    budgets = BudgetStore(bots)
    budgets.grant(parent.id, 100)
    request = spawns.propose(
        parent, _child_spec(harness="codex-native"), rationale="x", now=NOW, allowance=5
    )
    child = spawns.activate(request, decided_by="human:dpal", now=NOW)
    assert child.harness == "claude-native"


def test_a_childs_workspace_and_jar_are_derived_from_its_slug(
    bots: BotStore, spawns: SpawnStore
) -> None:
    parent = _configured_parent(bots)
    budgets = BudgetStore(bots)
    budgets.grant(parent.id, 100)
    request = spawns.propose(parent, _child_spec(), rationale="x", now=NOW, allowance=5)
    child = spawns.activate(request, decided_by="human:dpal", now=NOW)
    assert child.workspace is None
    assert child.browser_profile == "persist:bot-helper"


def test_a_person_writing_yaml_keeps_every_field(bots: BotStore) -> None:
    """The restriction is on bot-authored definitions, not on the format.

    A person already has the filesystem; withholding a field from them would be
    security theatre that costs them a real capability.
    """
    from army.bots.definition import to_bot

    human = to_bot(
        _child_spec(workspace="/srv/scout", browser_profile="persist:shared", docs_ref="git@x"),
        created_by="human:dpal",
        now=NOW,
    )
    assert human.workspace == "/srv/scout"
    assert human.browser_profile == "persist:shared"
    assert human.docs_ref == "git@x"


@pytest.mark.parametrize("bad", ["../escape", "has space", "UPPER", "a", "x" * 40, "1leading"])
def test_a_slug_cannot_be_a_path(bad: str, bots: BotStore, spawns: SpawnStore) -> None:
    """It is used as a directory name and a partition key, so it may contain
    nothing that would need escaping in either."""
    parent = _configured_parent(bots)
    with pytest.raises(SpawnRefused, match="slug"):
        spawns.propose(parent, _child_spec(slug=bad), rationale="x", now=NOW)


def test_drafts_count_against_the_fleet_cap(bots: BotStore, spawns: SpawnStore) -> None:
    """Counting only ACTIVE made drafts free.

    A parent could stack up proposals and the cap only bit at the very last
    switch-on — by which point a person has already approved them all.
    """
    for index in range(MAX_ACTIVE_BOTS):
        bots.create(make_bot(f"draft-{index}", workload=HEARTBEAT))
    parent = bots.by_slug("draft-0")
    assert parent is not None
    with pytest.raises(SpawnRefused, match="not retired"):
        spawns.propose(parent, _spec("eleven"), rationale="x", now=NOW)


def test_a_retired_bot_frees_a_slot(bots: BotStore, spawns: SpawnStore) -> None:
    """The cap is on the live fleet, not on everything that ever existed."""
    for index in range(MAX_ACTIVE_BOTS):
        bots.create(make_bot(f"draft-{index}", workload=HEARTBEAT))
    doomed = bots.by_slug("draft-9")
    assert doomed is not None
    bots.set_status(doomed, BotStatus.RETIRED, now=NOW)

    parent = bots.by_slug("draft-0")
    assert parent is not None
    spawns.propose(parent, _spec("eleven"), rationale="x", now=NOW)


def test_the_retire_cascade_terminates_even_on_a_cycle(bots: BotStore) -> None:
    """ "Should be impossible" is not a termination condition, and this walk runs
    every time somebody retires a bot."""
    first = activate(bots, make_bot("first", workload=HEARTBEAT), now=NOW)
    second = activate(bots, make_bot("second", workload=HEARTBEAT), now=NOW)
    with bots.store.atomic() as conn:
        conn.execute("UPDATE bots SET parent_bot_id = ? WHERE id = ?", (first.id, second.id))
        conn.execute("UPDATE bots SET parent_bot_id = ? WHERE id = ?", (second.id, first.id))

    retired = retire_descendants(bots, None, first, now=NOW + 1)
    assert {bot.slug for bot in retired} == {"second"}
