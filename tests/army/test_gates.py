"""Adversarial tests for the capability boundary.

These are deliberately not happy-path tests. A gate that only proves "approving
works" proves nothing about a gate — what matters is every way a grant can be
made to authorise something the owner did not agree to. Each test here is one
such attempt, and each must be refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from army.gates import (
    ALWAYS_OWNER,
    Broker,
    GateRefused,
    Grant,
    assert_agent_environment_is_clean,
    digest,
)
from army.store import Store

KEY = "test-broker-key"
MERGE = {"repo": "acme/widgets", "number": 42}


def _always_spends(nonce: str, operation: str) -> bool:
    """A spender that never sees a repeat — for tests about other properties."""
    return True


@pytest.fixture()
def broker() -> Broker:
    """A broker with a known signing key."""
    return Broker(KEY)


def test_a_granted_operation_passes(broker: Broker) -> None:
    """The gate does let through the exact thing that was granted."""
    grant = broker.sign("merge_pr", MERGE, now=1_000)
    broker.check("merge_pr", MERGE, grant, now=1_100)


def test_no_grant_is_refused(broker: Broker) -> None:
    """Silence is not consent."""
    with pytest.raises(GateRefused, match="needs an owner grant"):
        broker.check("merge_pr", MERGE, None)


def test_a_grant_cannot_be_replayed_on_different_arguments(broker: Broker) -> None:
    """Approving PR 42 must not approve PR 43.

    This is the whole reason the digest covers the arguments. Without it, one
    approval becomes a standing permission for the verb.
    """
    grant = broker.sign("merge_pr", MERGE, now=1_000)

    with pytest.raises(GateRefused, match="different merge_pr"):
        broker.check("merge_pr", {"repo": "acme/widgets", "number": 43}, grant, now=1_100)


def test_a_grant_cannot_be_replayed_under_a_different_verb(broker: Broker) -> None:
    """A dry-run approval must not authorise a live order."""
    grant = broker.sign("dry_run", MERGE, now=1_000)

    with pytest.raises(GateRefused, match="grant is for 'dry_run'"):
        broker.check("execute_order", MERGE, grant, now=1_100)


def test_an_expired_grant_is_refused(broker: Broker) -> None:
    """Consent given this morning is not consent for tonight."""
    grant = broker.sign("merge_pr", MERGE, ttl_seconds=60, now=1_000)

    with pytest.raises(GateRefused, match="expired"):
        broker.check("merge_pr", MERGE, grant, now=1_100)


def test_a_forged_signature_is_refused(broker: Broker) -> None:
    """Knowing the shape of a grant must not be enough to make one."""
    forged = Grant(
        operation="execute_order",
        digest=digest("execute_order", {"symbol": "BTC", "size": 10}),
        expires_at=9_999_999_999,
        nonce="deadbeef",
        signature="0" * 64,
    )

    with pytest.raises(GateRefused, match="signature does not verify"):
        broker.check("execute_order", {"symbol": "BTC", "size": 10}, forged, now=1_000)


def test_a_grant_from_another_key_is_refused() -> None:
    """A broker compromised elsewhere must not authorise anything here."""
    theirs = Broker("some-other-key", spender=_always_spends).sign(
        "execute_order", {"symbol": "BTC"}, now=1_000, owner_confirmed=True
    )

    with pytest.raises(GateRefused, match="signature does not verify"):
        Broker(KEY, spender=_always_spends).check(
            "execute_order", {"symbol": "BTC"}, theirs, now=1_100
        )


def test_extending_the_expiry_invalidates_the_grant(broker: Broker) -> None:
    """The expiry is signed, so it cannot be edited in transit."""
    grant = broker.sign("merge_pr", MERGE, ttl_seconds=60, now=1_000)
    extended = Grant(
        grant.operation, grant.digest, grant.expires_at + 86_400, grant.nonce, grant.signature
    )

    with pytest.raises(GateRefused, match="signature does not verify"):
        broker.check("merge_pr", MERGE, extended, now=1_100)


def test_a_token_round_trips(broker: Broker) -> None:
    """A grant survives being pasted through a chat reply."""
    grant = broker.sign("merge_pr", MERGE, now=1_000)

    broker.check("merge_pr", MERGE, Grant.from_token(grant.to_token()), now=1_100)


@pytest.mark.parametrize("token", ["", "nonsense", "a.b.c", "a.b.c.d.e", "merge.x.notanint.y"])
def test_a_malformed_token_is_refused(token: str) -> None:
    """Garbage in the grant field must not crash into a permissive path."""
    with pytest.raises(GateRefused):
        Grant.from_token(token)


def test_a_broker_without_a_key_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker that cannot verify grants would approve everything."""
    monkeypatch.delenv("ARMY_BROKER_KEY", raising=False)

    with pytest.raises(GateRefused, match="refusing to start"):
        Broker()


def test_the_never_automated_list_covers_the_three_settled_gates() -> None:
    """These are the owner's settled risk posture, not tunable defaults."""
    assert set(ALWAYS_OWNER) == {"add_dependency", "execute_order", "spend"}


def test_environment_check_finds_a_reachable_model_credential() -> None:
    """'No key in my shell' is not provenance; this checks a real environment."""
    leaked = assert_agent_environment_is_clean(
        {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-x", "HOME": "/home/agent"}
    )

    assert leaked == ["ANTHROPIC_API_KEY"]


def test_environment_check_passes_a_clean_environment() -> None:
    """A subscription-only session has nothing to find."""
    assert assert_agent_environment_is_clean({"PATH": "/usr/bin", "HOME": "/home/agent"}) == []


def test_environment_check_flags_the_grok_dual_auth_trap() -> None:
    """Grok's own auth hint offers OAuth *or* XAI_API_KEY.

    The second half of that hint is exactly the shortcut that turns a
    subscription-only deployment into a keyed one without anyone deciding to.
    """
    leaked = assert_agent_environment_is_clean({"XAI_API_KEY": "xai-x"})

    assert leaked == ["XAI_API_KEY"]


# ── one-shot grants ────────────────────────────────────────


def test_a_grant_is_spent_on_first_use(tmp_path: Path) -> None:
    """Binding to exact arguments stops the operation being *changed*.

    It does nothing about it being *repeated*. Approving one transfer of
    £10,000 must not authorise that same transfer for the rest of the hour,
    which is what a validate-only HMAC gives you.
    """
    store = Store(tmp_path / "army.db")
    broker = Broker(KEY, spender=store.consume_grant)
    grant = broker.sign(
        "execute_order", {"symbol": "BTC", "size": 1}, now=1_000, owner_confirmed=True
    )

    broker.check("execute_order", {"symbol": "BTC", "size": 1}, grant, now=1_001)

    with pytest.raises(GateRefused, match="already been used"):
        broker.check("execute_order", {"symbol": "BTC", "size": 1}, grant, now=1_002)


def test_a_spent_grant_stays_spent_across_a_restart(tmp_path: Path) -> None:
    """The whole point of spending it durably rather than in memory."""
    db = tmp_path / "army.db"
    grant = Broker(KEY, spender=Store(db).consume_grant).sign(
        "spend", {"amount": 500}, now=1_000, owner_confirmed=True
    )
    Broker(KEY, spender=Store(db).consume_grant).check("spend", {"amount": 500}, grant, now=1_001)

    with pytest.raises(GateRefused, match="already been used"):
        Broker(KEY, spender=Store(db).consume_grant).check(
            "spend", {"amount": 500}, grant, now=1_002
        )


def test_a_refused_check_does_not_spend_the_grant(tmp_path: Path) -> None:
    """A grant must not be burned by a call that was going to be refused."""
    store = Store(tmp_path / "army.db")
    broker = Broker(KEY, spender=store.consume_grant)
    grant = broker.sign("spend", {"amount": 500}, now=1_000, owner_confirmed=True)

    with pytest.raises(GateRefused, match="different spend"):
        broker.check("spend", {"amount": 999}, grant, now=1_001)

    broker.check("spend", {"amount": 500}, grant, now=1_002)


# ── the never-automated list is enforced, not decorative ───


def test_an_owner_only_verb_cannot_be_granted_automatically() -> None:
    """A list nothing consults is a comment.

    Any path that can reach ``sign`` could otherwise mint itself permission to
    spend money — the exact thing the list exists to prevent.
    """
    broker = Broker(KEY, spender=_always_spends)

    for operation in ALWAYS_OWNER:
        with pytest.raises(GateRefused, match="owner-only"):
            broker.sign(operation, {"x": 1}, now=1_000)


def test_an_owner_only_verb_refuses_a_broker_that_cannot_spend() -> None:
    """Fail closed: no way to spend a grant means no one-shot guarantee."""
    broker = Broker(KEY)

    with pytest.raises(GateRefused, match="nowhere to spend"):
        broker.sign("execute_order", {"x": 1}, now=1_000, owner_confirmed=True)


def test_a_reversible_verb_still_works_without_a_spender() -> None:
    """Only the money verbs demand one-shot; merging is not in that class."""
    Broker(KEY).check("merge_pr", MERGE, Broker(KEY).sign("merge_pr", MERGE, now=1_000), now=1_001)


# ── the environment check ──────────────────────────────────


def test_the_environment_check_flags_broker_secrets_too() -> None:
    """This function inspects an *agent's* environment.

    Finding ``ARMY_BROKER_KEY`` there is the single worst outcome — it means
    the broker is in the same process as the thing it is supposed to constrain.
    Skipping it made the check unable to see the failure it exists to catch.
    """
    leaked = assert_agent_environment_is_clean(
        {"PATH": "/usr/bin", "ARMY_BROKER_KEY": "k", "ARMY_GITHUB_TOKEN": "t"}
    )

    assert leaked == ["ARMY_BROKER_KEY", "ARMY_GITHUB_TOKEN"]
