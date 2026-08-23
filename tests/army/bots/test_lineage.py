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
