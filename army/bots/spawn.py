"""A bot may define a bot. Only a human may switch one on.

## Not reachable by a bot today, deliberately

``propose`` has no production caller: nothing gives a bot a way to invoke it,
and the only callers are tests. That is a **feature claim this module does not
currently deliver**, and it is written here rather than left to be discovered,
because five other helpers in this codebase read as working features while
having no callers at all.

It stays unwired on advice rather than by neglect. A definition is data that
names importable code, and the registry allowlist is the only thing standing
between "a bot proposed a bot" and a capability escalation wearing a
scheduler's clothes. The safe shape — a proposal that names an operator-owned
template and bounded parameters, never a definition body — is a policy review
nobody has asked to pay for. Until somebody wants bot-proposed bots badly
enough to fund that, this is a human-only path: an operator writes a
definition, and ``activate`` enforces the caps below.

That single rule is what makes runaway self-replication *structurally*
impossible rather than merely discouraged, and it is why this module exists at
all rather than a `create_bot` tool. A bot writes a definition and asks; the
definition lands as a real row in ``DRAFT``; nothing runs until a person
activates it.

The caps behind that rule are belt and braces, and each one closes a different
hole:

- **depth ≤ 2** — a child may not create grandchildren, so the tree cannot
  deepen into a shape nobody sized for.
- **fan-out ≤ 3 per parent** — one bot cannot flood the roster by breadth.
- **≤ 10 active bots** — the whole fleet, matching the concurrent-session
  ceiling the box was sized for.
- **budget carved from the parent** — the one that actually bites: capacity
  divides rather than multiplies, so a runaway starves itself.
- **TTL** — a child with no reason to exist retires without being noticed.
- **cascade** — retiring a parent retires its descendants, so there is no
  orphan fleet running under a bot nobody is watching.

A bounded sub-agent inside one run is not a Bot and needs none of this. Getting
a ``bots`` row is what triggers it.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from army.bots.budget import BudgetStore
from army.bots.definition import InvalidDefinition, to_bot
from army.bots.model import (
    MAX_ACTIVE_BOTS,
    MAX_DEPTH,
    MAX_FANOUT,
    Bot,
    BotStatus,
    dumps,
)
from army.bots.store import BotStore, _statements
from army.state import loads

_logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spawn_requests (
    id         TEXT PRIMARY KEY,
    parent_bot_id TEXT NOT NULL,
    run_id     TEXT,
    slug       TEXT NOT NULL,
    definition TEXT NOT NULL,
    rationale  TEXT NOT NULL,
    allowance  INTEGER NOT NULL DEFAULT 0,
    state      TEXT NOT NULL,
    child_bot_id TEXT,
    refused_because TEXT,
    decided_by TEXT,
    decided_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_spawn_open ON spawn_requests (state, created_at);
CREATE INDEX IF NOT EXISTS ix_spawn_parent ON spawn_requests (parent_bot_id, state);
"""

#: Iterations a child gets when its parent does not say. Small on purpose: a
#: proposal that has not thought about cost has not thought about the child.
DEFAULT_CHILD_ALLOWANCE = 20


class SpawnState(str, Enum):
    """Where a proposal has got to."""

    PROPOSED = "proposed"
    ACTIVATED = "activated"
    REFUSED = "refused"
    WITHDRAWN = "withdrawn"


class SpawnRefused(RuntimeError):
    """A proposal cannot become a bot. The message says which cap it hit."""


@dataclass(frozen=True)
class SpawnRequest:
    """A bot's proposal that another bot should exist.

    :param id: Stable id.
    :param parent_bot_id: Who proposed it.
    :param slug: The name asked for.
    :param definition: The full proposed definition.
    :param rationale: Why, in the parent's own words — the thing a person
        actually reads before deciding.
    :param allowance: Iterations to carve from the parent.
    :param state: Where it has got to.
    :param run_id: The iteration that proposed it.
    :param child_bot_id: The bot it became.
    :param refused_because: Why it was turned down.
    :param decided_by: Who decided.
    :param decided_at: When.
    :param created_at: Epoch seconds.
    """

    id: str
    parent_bot_id: str
    slug: str
    definition: dict[str, Any]
    rationale: str
    allowance: int
    state: SpawnState
    run_id: str | None = None
    child_bot_id: str | None = None
    refused_because: str | None = None
    decided_by: str | None = None
    decided_at: int | None = None
    created_at: int = 0


class SpawnStore:
    """Proposals, and the caps that decide whether one may become a bot.

    :param bots: Where bots live.
    :param budgets: The ledger a child's allowance is carved from.
    """

    def __init__(self, bots: BotStore, budgets: BudgetStore | None = None) -> None:
        self.bots = bots
        self.store = bots.store
        self.budgets = budgets
        with self.store.atomic() as conn:
            for statement in _statements(_SCHEMA):
                conn.execute(statement)

    def propose(
        self,
        parent: Bot,
        spec: dict[str, Any],
        *,
        rationale: str,
        now: int,
        allowance: int = DEFAULT_CHILD_ALLOWANCE,
        run_id: str | None = None,
    ) -> SpawnRequest:
        """
        Record a bot's proposal that another bot should exist.

        The caps are checked *here*, at proposal time, as well as at
        activation. Not because proposing is dangerous — it is a row — but
        because a proposal that could never be activated is a question wasting
        a person's attention, and the parent deserves to be told why now rather
        than after a human has read it.

        :param parent: The proposing bot.
        :param spec: The proposed definition, in the same format a person writes.
        :param rationale: Why this bot should exist.
        :param now: Epoch seconds.
        :param allowance: Iterations to carve from the parent.
        :param run_id: The iteration proposing it.
        :returns: The proposal.
        :raises SpawnRefused: If a cap or the definition itself refuses it.
        """
        self._check_caps(parent)
        try:
            # Built here and thrown away, purely to validate. A proposal that
            # cannot become a bot must fail now, not when a person clicks yes.
            to_bot(spec, created_by=parent.address, now=now, parent=parent)
        except InvalidDefinition as exc:
            raise SpawnRefused(f"{parent.slug} proposed an invalid definition: {exc}") from exc

        request = SpawnRequest(
            id=uuid.uuid4().hex,
            parent_bot_id=parent.id,
            slug=str(spec.get("slug", "")),
            definition=spec,
            rationale=rationale,
            allowance=max(0, allowance),
            state=SpawnState.PROPOSED,
            run_id=run_id,
            created_at=now,
        )
        with self.store.atomic() as conn:
            conn.execute(
                "INSERT INTO spawn_requests (id, parent_bot_id, run_id, slug, definition,"
                " rationale, allowance, state, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    request.id,
                    request.parent_bot_id,
                    request.run_id,
                    request.slug,
                    dumps(request.definition),
                    request.rationale,
                    request.allowance,
                    request.state.value,
                    request.created_at,
                ),
            )
        return request

    def activate(
        self,
        request: SpawnRequest,
        *,
        decided_by: str,
        now: int,
        allowance: int | None = None,
    ) -> Bot:
        """
        Turn a proposal into a real bot, in ``DRAFT``.

        Draft, not active, even here. Approving the *proposal* says this bot
        should exist; switching it on is still a separate act, so a person who
        approves ten proposals in a row has not started ten bots.

        The whole thing is one transaction: the bot row, its first revision,
        and the allowance carved out of the parent. A child that exists with no
        budget, or a parent debited for a child that was never created, are
        both worse than failing.

        :param request: The proposal.
        :param decided_by: Who approved it.
        :param now: Epoch seconds.
        :param allowance: Override what the parent asked for.
        :returns: The new bot.
        :raises SpawnRefused: If a cap now refuses it, or it was already decided.
        """
        if request.state is not SpawnState.PROPOSED:
            raise SpawnRefused(f"proposal {request.id[:12]} is already {request.state.value}")
        parent = self.bots.get(request.parent_bot_id)
        if parent is None:
            raise SpawnRefused("the bot that proposed this no longer exists")

        child = to_bot(request.definition, created_by=parent.address, now=now, parent=parent)
        share = request.allowance if allowance is None else allowance

        with self.store.atomic() as conn:
            # Inside the transaction, not before it. Checking outside let two
            # different proposals for the same parent both see two children and
            # both create one, giving four — the CAS on the proposal only stops
            # the *same* proposal being activated twice.
            self._check_caps(parent, conn=conn)
            # Claim the proposal *first*, as a compare-and-swap on its state.
            # The check above read an in-memory object that may be minutes old,
            # so two people clicking approve would otherwise both get past it
            # and the second would fail on a duplicate slug — which reads as
            # "that name is taken" rather than "someone already did this".
            claimed = conn.execute(
                "UPDATE spawn_requests SET state = ?, child_bot_id = ?, decided_by = ?,"
                " decided_at = ? WHERE id = ? AND state = ?",
                (
                    SpawnState.ACTIVATED.value,
                    child.id,
                    decided_by,
                    now,
                    request.id,
                    SpawnState.PROPOSED.value,
                ),
            )
            if claimed.rowcount != 1:
                raise SpawnRefused(
                    f"proposal {request.id[:12]} was already decided by someone else"
                )
            self.bots.create(child, conn=conn)
            if share > 0:
                if self.budgets is None:
                    raise SpawnRefused(
                        "a child needs an allowance and there is no ledger to carve one from"
                    )
                self.budgets.carve(parent.id, child.id, share, now=now, conn=conn)
        _logger.info(
            "%s proposed %s; %s created it at depth %d with %d iterations",
            parent.slug,
            child.slug,
            decided_by,
            child.depth,
            share,
        )
        return child

    def refuse(self, request: SpawnRequest, *, decided_by: str, because: str, now: int) -> None:
        """
        Turn a proposal down, with a reason the parent can read.

        :param request: The proposal.
        :param decided_by: Who refused.
        :param because: Why — this goes back to the bot, so it can stop asking.
        :param now: Epoch seconds.
        """
        with self.store.atomic() as conn:
            conn.execute(
                "UPDATE spawn_requests SET state = ?, refused_because = ?, decided_by = ?,"
                " decided_at = ? WHERE id = ? AND state = ?",
                (
                    SpawnState.REFUSED.value,
                    because[:500],
                    decided_by,
                    now,
                    request.id,
                    SpawnState.PROPOSED.value,
                ),
            )

    def pending(self) -> list[SpawnRequest]:
        """
        Proposals waiting on a person, oldest first.

        :returns: The proposals.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT * FROM spawn_requests WHERE state = ? ORDER BY created_at",
                (SpawnState.PROPOSED.value,),
            ).fetchall()
        return [_row_to_request(row) for row in rows]

    def get(self, spawn_id: str) -> SpawnRequest | None:
        """
        Load one proposal by id or unambiguous prefix.

        :param spawn_id: The id.
        :returns: The proposal, or ``None``.
        """
        with self.store.atomic() as conn:
            row = conn.execute("SELECT * FROM spawn_requests WHERE id = ?", (spawn_id,)).fetchone()
            if row is None:
                rows = conn.execute(
                    "SELECT * FROM spawn_requests WHERE id LIKE ? LIMIT 2", (f"{spawn_id}%",)
                ).fetchall()
                if len(rows) != 1:
                    return None
                row = rows[0]
        return _row_to_request(row)

    def _check_caps(self, parent: Bot, *, conn: sqlite3.Connection | None = None) -> None:
        """
        Refuse a proposal that no cap would let through.

        :param parent: The proposing bot.
        :param conn: Join an open transaction. Required when called from inside
            one — every read here would otherwise open a second connection and
            wait out the busy timeout against the caller's own lock.
        :raises SpawnRefused: Naming the cap, so the answer is actionable.
        """
        if parent.depth >= MAX_DEPTH:
            raise SpawnRefused(
                f"{parent.slug} is at depth {parent.depth}; the cap is {MAX_DEPTH}, "
                "so a child of it would be a grandchild. Only a human may create one."
            )
        children = [
            child
            for child in self.bots.children(parent.id, conn=conn)
            if child.status is not BotStatus.RETIRED
        ]
        if len(children) >= MAX_FANOUT:
            raise SpawnRefused(
                f"{parent.slug} already has {len(children)} live children "
                f"({', '.join(c.slug for c in children)}); the cap is {MAX_FANOUT}. "
                "Retire one before proposing another."
            )
        # Non-retired, not merely active. Counting only ACTIVE made drafts free,
        # so a parent could stack up proposals and the cap only bit at the very
        # last switch-on — by which point a person has approved them all.
        alive = [bot for bot in self.bots.list(conn=conn) if bot.status is not BotStatus.RETIRED]
        if len(alive) >= MAX_ACTIVE_BOTS:
            raise SpawnRefused(
                f"the fleet already holds {len(alive)} bots that are not retired; the cap "
                f"is {MAX_ACTIVE_BOTS}, which is what the box was sized for."
            )


def retire_descendants(
    bots: BotStore,
    budgets: BudgetStore | None,
    root: Bot,
    *,
    now: int,
) -> list[Bot]:
    """
    Retire a bot's children, and their children, returning their allowances.

    A retired parent whose children keep running is a fleet nobody is watching:
    the operator has stopped the thing they know about and left the ones it
    created going. Cascading is what makes "retire" mean what people assume.

    :param bots: Where bots live.
    :param budgets: The ledger, so unused allowance goes back.
    :param root: The bot being retired.
    :param now: Epoch seconds.
    :returns: The descendants that were retired.
    """
    retired: list[Bot] = []
    # A `parent_bot_id` cycle should be impossible — the depth cap and the
    # activation path both forbid it — but "should be impossible" is not a
    # termination condition, and this walk is what a retire runs.
    seen: set[str] = {root.id}
    frontier = bots.children(root.id)
    failed: list[str] = []
    while frontier:
        child = frontier.pop()
        if child.id in seen:
            continue
        seen.add(child.id)
        if child.status is BotStatus.RETIRED:
            continue
        frontier.extend(bots.children(child.id))
        parent_id = child.parent_bot_id
        try:
            bots.set_status(child, BotStatus.RETIRED, now=now, reason="its parent was retired")
        except Exception:
            _logger.exception("could not retire %s while cascading from %s", child.slug, root.slug)
            failed.append(child.slug)
            continue
        retired.append(child)
        if budgets is not None and parent_id is not None:
            account = budgets.account(child.id)
            if account is not None:
                budgets.release(parent_id, account.remaining, now=now)
    if failed:
        # The caller believes the cascade succeeded otherwise, and a descendant
        # left running under a retired parent is a bot nobody is watching.
        _logger.error(
            "retiring %s left %s running; retire them by hand", root.slug, ", ".join(failed)
        )
    return retired


def _row_to_request(row: sqlite3.Row) -> SpawnRequest:
    """Rebuild a :class:`SpawnRequest` from its row."""
    return SpawnRequest(
        id=row["id"],
        parent_bot_id=row["parent_bot_id"],
        slug=row["slug"],
        definition=loads(row["definition"], {}),
        rationale=row["rationale"],
        allowance=row["allowance"],
        state=SpawnState(row["state"]),
        run_id=row["run_id"],
        child_bot_id=row["child_bot_id"],
        refused_because=row["refused_because"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        created_at=row["created_at"],
    )
