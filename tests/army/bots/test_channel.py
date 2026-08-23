"""The channel, the delivery guarantee, and the bindings a verdict must satisfy.

Two claims are load-bearing here and both are about what survives a crash:
a message is redelivered rather than lost because the ack is written with the
work it acknowledges, and an approval authorises one exact operation rather
than "yes".
"""

from __future__ import annotations

import pytest

from army.bots.approvals import (
    ITERATION_GATE,
    ApprovalRefused,
    ApprovalState,
    ApprovalStore,
    owner_broker,
)
from army.bots.messages import MAX_ATTEMPTS, DeliveryState, MessageKind, MessageStore
from army.bots.store import BotStore
from army.gates import ALWAYS_OWNER, Broker, digest
from army.state import CommandKind
from army.store import ConcurrentTransition, Store

NOW = 1_700_000_000


@pytest.fixture()
def channel(bots: BotStore) -> MessageStore:
    return MessageStore(bots)


@pytest.fixture()
def approvals(bots: BotStore) -> ApprovalStore:
    return ApprovalStore(bots)


# ── the channel ───────────────────────────────────────────────────


def test_sequence_numbers_come_from_a_counter_not_a_max(
    channel: MessageStore, bots: BotStore
) -> None:
    """``MAX(seq) + 1`` read on its own connection loses a message under a race.

    The counter is bumped inside the insert's transaction, so two writers get
    two numbers rather than one number and one ``IntegrityError``.
    """
    seqs = [
        channel.post("bot", "human:a", MessageKind.HUMAN_MSG, f"m{i}", now=NOW).seq
        for i in range(5)
    ]
    assert seqs == [1, 2, 3, 4, 5]

    # A second bot's channel numbers independently.
    assert channel.post("other", "human:a", MessageKind.HUMAN_MSG, "x", now=NOW).seq == 1


def test_a_reader_resumes_from_its_cursor(channel: MessageStore) -> None:
    """Replayable across a restart, which an in-memory queue is not."""
    for index in range(5):
        channel.post("bot", "human:a", MessageKind.HUMAN_MSG, f"m{index}", now=NOW)
    assert [m.body for m in channel.channel("bot", after_seq=3)] == ["m3", "m4"]


def test_a_leased_message_comes_back_if_it_is_never_acked(channel: MessageStore) -> None:
    """A message handed to a body that dies must be redelivered, not lost."""
    message = channel.post(
        "bot", "human:a", MessageKind.HUMAN_MSG, "do it", now=NOW, deliver_to=["bot:b"]
    )
    first = channel.lease("bot:b", now=NOW)
    assert [d.message.id for d in first] == [message.id]

    # Still leased, so nothing else claims it.
    assert channel.lease("bot:b", now=NOW + 1) == []

    # The lease expires and it comes back, with the attempt counted.
    again = channel.lease("bot:b", now=NOW + 10_000)
    assert [d.message.id for d in again] == [message.id]
    assert again[0].attempts == 2


def test_an_ack_written_with_the_work_is_not_redelivered(
    channel: MessageStore, bots: BotStore
) -> None:
    """The delivery guarantee: the ack and the effect land in one transaction."""
    message = channel.post(
        "bot", "human:a", MessageKind.HUMAN_MSG, "do it", now=NOW, deliver_to=["bot:b"]
    )
    channel.lease("bot:b", now=NOW)
    with bots.atomic() as conn:
        # Whatever the receiver's own state change is, it goes here too.
        channel.ack(message.id, "bot:b", conn=conn)
    assert channel.lease("bot:b", now=NOW + 10_000) == []
    assert channel.pending_for("bot:b", now=NOW + 10_000) == 0


def test_an_ack_rolled_back_with_its_work_leaves_the_message_owed(
    channel: MessageStore, bots: BotStore
) -> None:
    """The other half: if the work did not land, the ack must not either."""
    message = channel.post(
        "bot", "human:a", MessageKind.HUMAN_MSG, "do it", now=NOW, deliver_to=["bot:b"]
    )
    channel.lease("bot:b", now=NOW)
    with pytest.raises(RuntimeError):
        with bots.atomic() as conn:
            channel.ack(message.id, "bot:b", conn=conn)
            raise RuntimeError("the work failed after the ack was written")
    assert [d.message.id for d in channel.lease("bot:b", now=NOW + 10_000)] == [message.id]


def test_a_message_that_keeps_failing_is_parked_rather_than_starving_the_queue(
    channel: MessageStore,
) -> None:
    """Redelivering forever starves everything behind it."""
    channel.post("bot", "human:a", MessageKind.HUMAN_MSG, "poison", now=NOW, deliver_to=["bot:b"])
    clock = NOW
    for _ in range(MAX_ATTEMPTS):
        assert channel.lease("bot:b", now=clock) != []
        clock += 10_000
    assert channel.lease("bot:b", now=clock) == []

    dead = channel.dead_letters()
    assert len(dead) == 1
    assert dead[0][1] == "bot:b"
    assert "gave up" in dead[0][2]


def test_the_fleet_wide_waiting_scan_is_one_query(channel: MessageStore) -> None:
    """Asking per bot is what makes a forty-bot roster slow when it is busy."""
    channel.post("a", "human:x", MessageKind.HUMAN_MSG, "1", now=NOW, deliver_to=["bot:a"])
    channel.post("a", "human:x", MessageKind.HUMAN_MSG, "2", now=NOW, deliver_to=["bot:a"])
    channel.post("b", "human:x", MessageKind.HUMAN_MSG, "3", now=NOW, deliver_to=["bot:b"])
    assert channel.waiting_recipients(now=NOW) == {"bot:a": 2, "bot:b": 1}


def test_a_message_nobody_owes_an_ack_on_is_readable_but_not_deliverable(
    channel: MessageStore,
) -> None:
    """A report is there to be read, not acted on."""
    channel.post("bot", "bot:bot", MessageKind.REPORT, "weekly", now=NOW)
    assert channel.waiting_recipients(now=NOW) == {}
    assert len(channel.channel("bot")) == 1


# ── approvals ─────────────────────────────────────────────────────


def _ask(approvals: ApprovalStore, **overrides: object) -> object:
    params: dict = {
        "bot_id": "bot",
        "run_id": "run",
        "run_version": 3,
        "verb": ITERATION_GATE,
        "parameters": {"iteration": 814},
        "question": "Merge?",
        "options": ["merge", "iterate"],
        "now": NOW,
    }
    params.update(overrides)  # type: ignore[arg-type]
    return approvals.request(**params)  # type: ignore[arg-type]


def test_only_a_decision_creates_a_command(approvals: ApprovalStore, store: Store) -> None:
    """Nothing written at ask time may be mistaken for the answer.

    The existing ``commands`` table has no pending state and the supervisor
    consumes the oldest unconsumed command *as the verdict*, so a row written
    when the question is asked would immediately answer it.
    """
    request = _ask(approvals)
    assert store.next_command("run") is None, "asking created something answerable"

    command = approvals.decide(
        request, approved=True, decided_by="human:dpal", now=NOW, choice="merge"
    )
    assert command.kind is CommandKind.APPROVE
    assert store.next_command("run") is not None


def test_a_verdict_whose_action_changed_is_re_asked_not_honoured(
    approvals: ApprovalStore,
) -> None:
    """A bare yes would authorise whatever the fresh plan turns out to be."""
    request = _ask(approvals)
    with pytest.raises(ApprovalRefused, match="the action changed"):
        approvals.decide(
            request,
            approved=True,
            decided_by="human:dpal",
            now=NOW,
            choice="merge",
            verb=ITERATION_GATE,
            parameters={"iteration": 815},
        )


def test_a_verdict_that_outlived_its_policy_is_re_asked(approvals: ApprovalStore) -> None:
    request = _ask(approvals, policy_version="v4")
    with pytest.raises(ApprovalRefused, match="policy moved"):
        approvals.decide(
            request,
            approved=True,
            decided_by="human:dpal",
            now=NOW,
            choice="merge",
            policy_version="v5",
        )


def test_a_verdict_for_an_iteration_that_moved_on_is_re_asked(
    approvals: ApprovalStore,
) -> None:
    request = _ask(approvals, run_version=3)
    with pytest.raises(ApprovalRefused, match="run moved"):
        approvals.decide(
            request, approved=True, decided_by="human:dpal", now=NOW, choice="merge", run_version=4
        )


def test_a_denial_does_not_need_the_bindings_to_still_hold(
    approvals: ApprovalStore,
) -> None:
    """Refusing an operation that changed is still a refusal.

    Demanding a fresh question before someone may say no is how a gate becomes
    the thing people route around.
    """
    request = _ask(approvals)
    command = approvals.decide(
        request,
        approved=False,
        decided_by="human:dpal",
        now=NOW,
        verb=ITERATION_GATE,
        parameters={"iteration": 999},
    )
    assert command.kind is CommandKind.DENY


def test_the_same_question_cannot_be_answered_twice(approvals: ApprovalStore) -> None:
    request = _ask(approvals)
    approvals.decide(request, approved=True, decided_by="human:a", now=NOW, choice="merge")
    reread = approvals.get(request.id)
    assert reread is not None
    with pytest.raises(ApprovalRefused, match="not open"):
        approvals.decide(reread, approved=True, decided_by="human:b", now=NOW, choice="merge")


def test_two_people_answering_at_once_produce_one_verdict(
    approvals: ApprovalStore,
) -> None:
    """Both read the pending row; the compare-and-swap picks a winner."""
    request = _ask(approvals)
    stale = approvals.get(request.id)
    assert stale is not None
    approvals.decide(request, approved=True, decided_by="human:a", now=NOW, choice="merge")
    with pytest.raises((ApprovalRefused, ConcurrentTransition)):
        approvals.decide(stale, approved=False, decided_by="human:b", now=NOW)


def test_an_unoffered_choice_is_refused(approvals: ApprovalStore) -> None:
    """A typo is not an answer, and defaulting one to merge is the worst guess."""
    request = _ask(approvals)
    with pytest.raises(ApprovalRefused, match="not one of the offered"):
        approvals.decide(request, approved=True, decided_by="human:dpal", now=NOW, choice="mrege")


def test_expiry_pauses_rather_than_approves(approvals: ApprovalStore) -> None:
    """A system that approves on silence makes a holiday a blanket authorisation."""
    request = _ask(approvals, ttl_seconds=60)
    assert approvals.expire_due(now=NOW + 30) == []

    expired = approvals.expire_due(now=NOW + 61)
    assert [r.id for r in expired] == [request.id]

    reread = approvals.get(request.id)
    assert reread is not None and reread.state is ApprovalState.EXPIRED
    with pytest.raises(ApprovalRefused):
        approvals.decide(reread, approved=True, decided_by="human:dpal", now=NOW + 62)


# ── the owner boundary ────────────────────────────────────────────


@pytest.mark.parametrize("verb", sorted(ALWAYS_OWNER))
def test_a_channel_verdict_never_satisfies_an_owner_verb(
    approvals: ApprovalStore, verb: str
) -> None:
    """Settled risk posture, not a default to tune.

    ``spend``, ``execute_order`` and ``add_dependency`` are answerable only
    with a signed grant from the owner's own path.
    """
    request = _ask(approvals, verb=verb, parameters={"amount": 40})
    assert request.requires_owner
    with pytest.raises(ApprovalRefused, match="owner"):
        approvals.decide(request, approved=True, decided_by="human:dpal", now=NOW)


def test_an_owner_verb_is_refused_outright_when_no_broker_exists(
    approvals: ApprovalStore,
) -> None:
    """Refusing is safe; pretending there is a boundary is not."""
    assert approvals.broker is None
    request = _ask(approvals, verb="spend", parameters={"amount": 40})
    with pytest.raises(ApprovalRefused, match="no broker is configured"):
        approvals.decide(request, approved=True, decided_by="human:dpal", now=NOW)


def test_a_signed_owner_grant_satisfies_an_owner_verb_exactly_once(
    bots: BotStore,
) -> None:
    """The grant is a permission, not a capability: spending it is the lock."""
    broker = Broker("test-key", spender=bots.store.consume_grant)
    approvals = ApprovalStore(bots, broker)
    request = _ask(approvals, verb="spend", parameters={"amount": 40})

    grant = broker.sign(
        "spend", {"action_hash": request.action_hash}, now=NOW, owner_confirmed=True
    )
    command = approvals.decide(
        request, approved=True, decided_by="human:owner", now=NOW, grant=grant
    )
    assert command.kind is CommandKind.APPROVE

    # The same grant against a second question loses: the nonce is spent.
    second = _ask(approvals, verb="spend", parameters={"amount": 40}, run_id="run2")
    with pytest.raises(ApprovalRefused, match="already been used"):
        approvals.decide(second, approved=True, decided_by="human:owner", now=NOW, grant=grant)


def test_an_automated_path_cannot_mint_itself_an_owner_grant(bots: BotStore) -> None:
    """The flag is a marker that the call site is the owner path, not agent-reachable."""
    from army.gates import GateRefused

    broker = Broker("test-key", spender=bots.store.consume_grant)
    with pytest.raises(GateRefused, match="owner-only"):
        broker.sign("spend", {"amount": 40}, now=NOW)


def test_a_grant_for_a_different_operation_does_not_transfer(bots: BotStore) -> None:
    """A grant for one spend cannot be replayed against another."""
    broker = Broker("test-key", spender=bots.store.consume_grant)
    approvals = ApprovalStore(bots, broker)
    request = _ask(approvals, verb="spend", parameters={"amount": 40})
    elsewhere = broker.sign(
        "spend", {"action_hash": digest("spend", {"amount": 9999})}, now=NOW, owner_confirmed=True
    )
    with pytest.raises(ApprovalRefused, match="does not match"):
        approvals.decide(
            request, approved=True, decided_by="human:owner", now=NOW, grant=elsewhere
        )


def test_the_broker_is_actually_constructed_now(bots: BotStore, monkeypatch) -> None:
    """It was defined and never constructed, so there was no boundary at all."""
    monkeypatch.delenv("ARMY_BROKER_KEY", raising=False)
    assert owner_broker(bots) is None, "a keyless broker must refuse to exist"

    monkeypatch.setenv("ARMY_BROKER_KEY", "a-real-key")
    broker = owner_broker(bots)
    assert broker is not None
    grant = broker.sign("spend", {"x": 1}, now=NOW, owner_confirmed=True)
    # Spendable exactly once, through the store's own table.
    broker.check("spend", {"x": 1}, grant, now=NOW)
    with pytest.raises(Exception, match="already been used"):
        broker.check("spend", {"x": 1}, grant, now=NOW)


def test_an_open_question_is_findable_from_its_run(approvals: ApprovalStore) -> None:
    request = _ask(approvals)
    found = approvals.open_for_run("run")
    assert found is not None and found.id == request.id
    approvals.decide(request, approved=True, decided_by="human:a", now=NOW, choice="merge")
    assert approvals.open_for_run("run") is None


def test_deliveries_are_idempotent_on_the_message_id(channel: MessageStore) -> None:
    """At-least-once puts the burden on the id, so posting twice owes once."""
    message = channel.post(
        "bot", "human:a", MessageKind.HUMAN_MSG, "hi", now=NOW, deliver_to=["bot:b", "bot:b"]
    )
    assert channel.pending_for("bot:b", now=NOW) == 1
    leased = channel.lease("bot:b", now=NOW)
    assert len(leased) == 1
    assert leased[0].message.id == message.id
    assert leased[0].state is DeliveryState.LEASED
