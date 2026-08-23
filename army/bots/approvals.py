"""Approvals that mean one thing, once, and cannot be cashed for another.

A verdict is not "yes". A verdict is *yes, to this exact operation, under the
policy that was in force when it was asked, against the run version it was
asked at*. Anything looser is a permission slip with the amount left blank: the
plan that gets executed after the answer is a fresh plan, and a bare yes
authorises whatever that plan turns out to be.

So a verdict binds three things, and a mismatch on any of them re-asks rather
than being honoured:

- the **action hash** — :func:`army.gates.digest` over the verb and its exact
  arguments;
- the **policy version** in force at ask time;
- the **run version** it was asked at.

And three verbs are not answerable here at all. ``add_dependency``,
``execute_order`` and ``spend`` are :data:`army.gates.ALWAYS_OWNER`: a click in
a bot's channel never satisfies them. They route to the owner's signed channel,
come back as a one-shot :class:`army.gates.Grant`, and are spent through
``used_grants`` so the same approval cannot pay twice.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from army.bots.model import dumps
from army.bots.store import BotStore, _statements
from army.gates import ALWAYS_OWNER, Broker, GateRefused, Grant, digest
from army.state import Command, CommandKind, loads
from army.store import ConcurrentTransition

_logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    id             TEXT PRIMARY KEY,
    bot_id         TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    thread_id      TEXT,
    verb           TEXT NOT NULL,
    action_hash    TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    run_version    INTEGER NOT NULL,
    question       TEXT NOT NULL,
    options        TEXT NOT NULL DEFAULT '[]',
    evidence       TEXT NOT NULL DEFAULT '{}',
    state          TEXT NOT NULL,
    requires_owner INTEGER NOT NULL DEFAULT 0,
    escalations    INTEGER NOT NULL DEFAULT 0,
    choice         TEXT,
    decided_by     TEXT,
    decided_at     INTEGER,
    grant_nonce    TEXT,
    expires_at     INTEGER,
    created_at     INTEGER NOT NULL,
    version        INTEGER NOT NULL DEFAULT 0
);
-- The two scans: what is outstanding for a run, and what has expired.
CREATE INDEX IF NOT EXISTS ix_approvals_open ON approval_requests (state, expires_at);
CREATE INDEX IF NOT EXISTS ix_approvals_run ON approval_requests (run_id, state);
CREATE INDEX IF NOT EXISTS ix_approvals_bot ON approval_requests (bot_id, state, created_at);
"""

#: The verb an ordinary iteration gate asks under. Distinct from a real
#: capability so that "may this iteration continue" and "may you spend money"
#: are never the same question with a different label.
ITERATION_GATE = "iteration_gate"

#: How long an unanswered approval stays answerable. After this it expires and
#: the bot is paused with a report — never auto-approved, which the hard gates
#: in :mod:`army.gates` forbid outright.
DEFAULT_TTL_SECONDS = 86_400


class ApprovalState(str, Enum):
    """Where a request has got to."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalRefused(RuntimeError):
    """A verdict was rejected. The message says which binding failed."""


@dataclass(frozen=True)
class ApprovalRequest:
    """One outstanding question, and everything a verdict must match.

    :param id: Stable id.
    :param bot_id: Whose channel it lives in.
    :param run_id: The iteration waiting on it.
    :param verb: What is being asked for.
    :param action_hash: Fingerprint of the exact operation.
    :param policy_version: What was in force when it was asked.
    :param run_version: The run's version at ask time.
    :param question: What a person reads.
    :param options: The choices offered.
    :param evidence: What they should see before deciding.
    :param state: Where it has got to.
    :param requires_owner: Whether only a signed owner grant can satisfy it.
    :param escalations: How many times it has been re-pinged.
    :param thread_id: The channel thread it belongs to.
    :param choice: Which option was picked.
    :param decided_by: Who decided.
    :param decided_at: When.
    :param grant_nonce: The one-shot grant spent, for an owner verb.
    :param expires_at: When it stops being answerable.
    :param created_at: Epoch seconds.
    :param version: CAS fencing token.
    """

    id: str
    bot_id: str
    run_id: str
    verb: str
    action_hash: str
    policy_version: str
    run_version: int
    question: str
    options: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    state: ApprovalState = ApprovalState.PENDING
    requires_owner: bool = False
    escalations: int = 0
    thread_id: str | None = None
    choice: str | None = None
    decided_by: str | None = None
    decided_at: int | None = None
    grant_nonce: str | None = None
    expires_at: int | None = None
    created_at: int = 0
    version: int = 0

    @property
    def is_open(self) -> bool:
        """Whether a verdict would still be accepted."""
        return self.state is ApprovalState.PENDING


class ApprovalStore:
    """Outstanding questions, and the rules a verdict has to satisfy.

    :param bots: The bot store, whose database and transactions this shares —
        an approved verdict and the command it produces must land together.
    :param broker: The capability boundary for owner-only verbs. ``None``
        leaves those verbs unanswerable, which is the correct posture for a
        deployment that has not set ``ARMY_BROKER_KEY``: refusing is safe,
        pretending is not.
    """

    def __init__(self, bots: BotStore, broker: Broker | None = None) -> None:
        self.bots = bots
        self.store = bots.store
        self.broker = broker
        with self.store.atomic() as conn:
            for statement in _statements(_SCHEMA):
                conn.execute(statement)

    # ── asking ────────────────────────────────────────────────────

    def request(
        self,
        *,
        bot_id: str,
        run_id: str,
        run_version: int,
        verb: str,
        parameters: dict[str, Any],
        question: str,
        options: list[str],
        evidence: dict[str, Any] | None = None,
        policy_version: str = "v1",
        thread_id: str | None = None,
        now: int,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        conn: sqlite3.Connection | None = None,
    ) -> ApprovalRequest:
        """
        Record a question, bound to the operation it is asking about.

        :param bot_id: Whose channel.
        :param run_id: The iteration that will wait on it.
        :param run_version: The run's version now.
        :param verb: What is being asked for.
        :param parameters: The operation's exact arguments, which the hash
            covers — so a verdict cannot be replayed against different ones.
        :param question: What a person reads.
        :param options: The choices offered.
        :param evidence: What they should see first.
        :param policy_version: What is in force.
        :param thread_id: The channel thread.
        :param now: Epoch seconds.
        :param ttl_seconds: How long it stays answerable.
        :param conn: Join an open transaction, or ``None``.
        :returns: The pending request.
        """
        request = ApprovalRequest(
            id=uuid.uuid4().hex,
            bot_id=bot_id,
            run_id=run_id,
            verb=verb,
            action_hash=digest(verb, parameters),
            policy_version=policy_version,
            run_version=run_version,
            question=question,
            options=list(options),
            evidence=evidence or {},
            state=ApprovalState.PENDING,
            requires_owner=verb in ALWAYS_OWNER,
            thread_id=thread_id,
            expires_at=now + ttl_seconds,
            created_at=now,
        )
        with self.bots._tx(conn) as conn:
            conn.execute(
                "INSERT INTO approval_requests (id, bot_id, run_id, thread_id, verb,"
                " action_hash, policy_version, run_version, question, options, evidence,"
                " state, requires_owner, escalations, expires_at, created_at, version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,0)",
                (
                    request.id,
                    request.bot_id,
                    request.run_id,
                    request.thread_id,
                    request.verb,
                    request.action_hash,
                    request.policy_version,
                    request.run_version,
                    request.question,
                    dumps(request.options),
                    dumps(request.evidence),
                    request.state.value,
                    int(request.requires_owner),
                    request.expires_at,
                    request.created_at,
                ),
            )
        return request

    # ── answering ─────────────────────────────────────────────────

    def decide(
        self,
        request: ApprovalRequest,
        *,
        approved: bool,
        decided_by: str,
        now: int,
        choice: str | None = None,
        verb: str | None = None,
        parameters: dict[str, Any] | None = None,
        policy_version: str | None = None,
        run_version: int | None = None,
        grant: Grant | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Command:
        """
        Accept a verdict, if it still means what it meant when it was asked.

        Every check here exists because skipping it turns a specific answer
        into a general one. The action hash stops the answer being cashed
        against a different operation; the policy version stops it surviving a
        rule change; the run version stops it applying to an iteration that has
        moved on. A failure is not a denial — it re-asks, because the person
        answered honestly and the question changed underneath them.

        :param request: The pending request, as read.
        :param approved: The decision.
        :param decided_by: Who decided.
        :param now: Epoch seconds.
        :param choice: Which option, for a multi-option question.
        :param verb: The operation as it stands now. Omit to skip the re-check,
            which is only correct when nothing could have changed.
        :param parameters: Its arguments as they stand now.
        :param policy_version: What is in force now.
        :param run_version: The run's version now.
        :param grant: An owner grant, required for an
            :data:`army.gates.ALWAYS_OWNER` verb.
        :param conn: Join an open transaction, or ``None``.
        :returns: The durable command the supervisor will apply. **Only a
            decision creates one** — nothing is written at ask time that a tick
            could mistake for an answer.
        :raises ApprovalRefused: If any binding fails, or the request is closed.
        """
        if not request.is_open:
            raise ApprovalRefused(
                f"approval {request.id[:12]} is {request.state.value}, not open for a verdict"
            )
        if request.expires_at is not None and now >= request.expires_at:
            raise ApprovalRefused(
                f"approval {request.id[:12]} expired {now - request.expires_at}s ago; ask again"
            )

        if approved:
            self._check_bindings(
                request,
                verb=verb,
                parameters=parameters,
                policy_version=policy_version,
                run_version=run_version,
            )
            nonce = self._spend_owner_grant(request, grant, now=now)
        else:
            # A denial needs no binding check. Refusing an operation that has
            # since changed is still a refusal, and demanding a fresh question
            # before someone may say no is how a gate becomes a nuisance people
            # route around.
            nonce = None

        if choice is not None and request.options and choice not in request.options:
            raise ApprovalRefused(
                f"{choice!r} is not one of the offered options: {', '.join(request.options)}"
            )

        command = Command.new(
            request.run_id,
            CommandKind.APPROVE if approved else CommandKind.DENY,
            {"choice": choice} if choice else {},
            now=now,
        )
        target = ApprovalState.APPROVED if approved else ApprovalState.DENIED
        with self.bots._tx(conn) as conn:
            cursor = conn.execute(
                "UPDATE approval_requests SET state = ?, choice = ?, decided_by = ?,"
                " decided_at = ?, grant_nonce = ?, version = version + 1"
                " WHERE id = ? AND version = ? AND state = ?",
                (
                    target.value,
                    choice,
                    decided_by,
                    now,
                    nonce,
                    request.id,
                    request.version,
                    ApprovalState.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentTransition(
                    f"approval {request.id[:12]} was decided by someone else first"
                )
            self.store.record_command(command, conn=conn)
        return command

    def _check_bindings(
        self,
        request: ApprovalRequest,
        *,
        verb: str | None,
        parameters: dict[str, Any] | None,
        policy_version: str | None,
        run_version: int | None,
    ) -> None:
        """
        Refuse a verdict whose question has changed underneath it.

        :raises ApprovalRefused: Naming the binding that failed, so the
            operator can see whether the plan changed, the policy changed, or
            the run moved on.
        """
        if verb is not None and parameters is not None:
            current = digest(verb, parameters)
            if current != request.action_hash:
                raise ApprovalRefused(
                    f"the action changed while this sat waiting: approved "
                    f"{request.action_hash[:12]}, now {current[:12]}. Re-asking."
                )
        if policy_version is not None and policy_version != request.policy_version:
            raise ApprovalRefused(
                f"policy moved from {request.policy_version} to {policy_version} "
                "while this sat waiting. Re-asking."
            )
        if run_version is not None and run_version != request.run_version:
            raise ApprovalRefused(
                f"run moved from version {request.run_version} to {run_version} "
                "while this sat waiting. Re-asking."
            )

    def _spend_owner_grant(
        self, request: ApprovalRequest, grant: Grant | None, *, now: int
    ) -> str | None:
        """
        Require and spend a signed owner grant for an owner-only verb.

        This is the line a channel click may not cross. ``spend``,
        ``execute_order`` and ``add_dependency`` are settled risk posture, not
        a default to tune, and the grant is one-shot so the same approval
        cannot pay twice.

        :returns: The grant's nonce, recorded for the audit trail, or ``None``
            for an ordinary verb.
        :raises ApprovalRefused: If the verb needs a grant and does not have a
            valid, unspent one.
        """
        if not request.requires_owner:
            return None
        if self.broker is None:
            raise ApprovalRefused(
                f"{request.verb} is owner-only and no broker is configured. "
                "Set ARMY_BROKER_KEY and route this through the owner channel; "
                "a verdict in a bot's channel cannot authorise it."
            )
        if grant is None:
            raise ApprovalRefused(
                f"{request.verb} needs a signed owner grant; a channel verdict is not one"
            )
        try:
            # The broker re-derives the digest from the operation it is given,
            # so a grant for a different operation fails here rather than being
            # accepted because the approval row happened to match.
            self.broker.check(
                request.verb,
                {"action_hash": request.action_hash},
                grant,
                now=now,
            )
        except GateRefused as exc:
            raise ApprovalRefused(f"owner grant refused: {exc}") from exc
        return grant.nonce

    # ── housekeeping ──────────────────────────────────────────────

    def open_for_run(self, run_id: str) -> ApprovalRequest | None:
        """
        The question an iteration is currently waiting on.

        :param run_id: The run.
        :returns: The pending request, or ``None``.
        """
        with self.store.atomic() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE run_id = ? AND state = ?"
                " ORDER BY created_at DESC LIMIT 1",
                (run_id, ApprovalState.PENDING.value),
            ).fetchone()
        return _row_to_request(row) if row is not None else None

    def get(self, approval_id: str) -> ApprovalRequest | None:
        """
        Load one request by id or unambiguous prefix.

        :param approval_id: The id.
        :returns: The request, or ``None``.
        """
        with self.store.atomic() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                rows = conn.execute(
                    "SELECT * FROM approval_requests WHERE id LIKE ? LIMIT 2",
                    (f"{approval_id}%",),
                ).fetchall()
                if len(rows) != 1:
                    return None
                row = rows[0]
        return _row_to_request(row)

    def pending(self, *, bot_id: str | None = None) -> list[ApprovalRequest]:
        """
        Everything still waiting on a person, oldest first.

        :param bot_id: Restrict to one bot, or ``None`` for the fleet.
        :returns: The pending requests.
        """
        sql = "SELECT * FROM approval_requests WHERE state = ?"
        params: list[Any] = [ApprovalState.PENDING.value]
        if bot_id is not None:
            sql += " AND bot_id = ?"
            params.append(bot_id)
        with self.store.atomic() as conn:
            rows = conn.execute(sql + " ORDER BY created_at", params).fetchall()
        return [_row_to_request(row) for row in rows]

    def expire_due(self, *, now: int) -> list[ApprovalRequest]:
        """
        Close out questions nobody answered in time.

        Expiry is never an approval. The hard gates forbid it, and a system
        that approves on silence is one where going on holiday authorises
        everything. The bot is paused and reported instead.

        :param now: Epoch seconds.
        :returns: The requests that just expired, so the caller can pause their
            bots and say so.
        """
        with self.store.atomic() as conn:
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE state = ?"
                " AND expires_at IS NOT NULL AND expires_at <= ?",
                (ApprovalState.PENDING.value, now),
            ).fetchall()
            expired = [_row_to_request(row) for row in rows]
            for request in expired:
                conn.execute(
                    "UPDATE approval_requests SET state = ?, decided_at = ?,"
                    " version = version + 1 WHERE id = ? AND state = ?",
                    (
                        ApprovalState.EXPIRED.value,
                        now,
                        request.id,
                        ApprovalState.PENDING.value,
                    ),
                )
        return expired

    def cancel(self, request: ApprovalRequest, *, now: int, reason: str) -> None:
        """
        Withdraw a question whose answer could no longer be applied.

        :param request: The request.
        :param now: Epoch seconds.
        :param reason: Recorded as the choice, so the trail says why.
        """
        with self.store.atomic() as conn:
            conn.execute(
                "UPDATE approval_requests SET state = ?, choice = ?, decided_at = ?,"
                " version = version + 1 WHERE id = ? AND state = ?",
                (
                    ApprovalState.CANCELLED.value,
                    reason[:200],
                    now,
                    request.id,
                    ApprovalState.PENDING.value,
                ),
            )

    def escalate(self, request: ApprovalRequest, *, now: int) -> int:  # noqa: ARG002
        """
        Record that a question was re-pinged.

        :param request: The request.
        :param now: Epoch seconds, unused today but part of the contract for a
            cadence that varies by age.
        :returns: How many times it has now been escalated.
        """
        with self.store.atomic() as conn:
            conn.execute(
                "UPDATE approval_requests SET escalations = escalations + 1,"
                " version = version + 1 WHERE id = ?",
                (request.id,),
            )
        return request.escalations + 1


def owner_broker(bots: BotStore, key: str | None = None) -> Broker | None:
    """
    Construct the capability boundary, wired to a place grants can be spent.

    ``Broker`` has existed in :mod:`army.gates` and been constructed nowhere,
    which means the running loop has had no capability boundary at all — only a
    design for one. This is the constructor that was missing.

    The spender matters as much as the key. An HMAC that merely *validates* is
    a capability rather than a permission: it keeps working until it expires,
    so one approval of a transfer would authorise that transfer for the rest of
    the hour. Recording the nonce under a primary key makes the second attempt
    lose.

    :param bots: The bot store, for its ``used_grants`` table.
    :param key: Signing key, or ``None`` to read ``ARMY_BROKER_KEY``.
    :returns: The broker, or ``None`` when no key is configured — in which case
        owner-only verbs are refused rather than quietly permitted.
    """
    try:
        return Broker(key, spender=bots.store.consume_grant)
    except GateRefused as exc:
        _logger.warning(
            "no capability boundary: %s. Owner-only verbs (%s) will be refused.",
            exc,
            ", ".join(sorted(ALWAYS_OWNER)),
        )
        return None


def _row_to_request(row: sqlite3.Row) -> ApprovalRequest:
    """Rebuild an :class:`ApprovalRequest` from its row."""
    return ApprovalRequest(
        id=row["id"],
        bot_id=row["bot_id"],
        run_id=row["run_id"],
        verb=row["verb"],
        action_hash=row["action_hash"],
        policy_version=row["policy_version"],
        run_version=row["run_version"],
        question=row["question"],
        options=loads(row["options"], []),
        evidence=loads(row["evidence"], {}),
        state=ApprovalState(row["state"]),
        requires_owner=bool(row["requires_owner"]),
        escalations=row["escalations"],
        thread_id=row["thread_id"],
        choice=row["choice"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        grant_nonce=row["grant_nonce"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        version=row["version"],
    )
