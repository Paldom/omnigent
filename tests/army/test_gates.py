"""Adversarial tests for the capability boundary.

These are deliberately not happy-path tests. A gate that only proves "approving
works" proves nothing about a gate — what matters is every way a grant can be
made to authorise something the owner did not agree to. Each test here is one
such attempt, and each must be refused.
"""

from __future__ import annotations

import pytest

from army.gates import (
    ALWAYS_OWNER,
    Broker,
    GateRefused,
    Grant,
    assert_agent_environment_is_clean,
    digest,
)

KEY = "test-broker-key"
MERGE = {"repo": "acme/widgets", "number": 42}


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
        signature="0" * 64,
    )

    with pytest.raises(GateRefused, match="signature does not verify"):
        broker.check("execute_order", {"symbol": "BTC", "size": 10}, forged, now=1_000)


def test_a_grant_from_another_key_is_refused() -> None:
    """A broker compromised elsewhere must not authorise anything here."""
    theirs = Broker("some-other-key").sign("execute_order", {"symbol": "BTC"}, now=1_000)

    with pytest.raises(GateRefused, match="signature does not verify"):
        Broker(KEY).check("execute_order", {"symbol": "BTC"}, theirs, now=1_100)


def test_extending_the_expiry_invalidates_the_grant(broker: Broker) -> None:
    """The expiry is signed, so it cannot be edited in transit."""
    grant = broker.sign("merge_pr", MERGE, ttl_seconds=60, now=1_000)
    extended = Grant(grant.operation, grant.digest, grant.expires_at + 86_400, grant.signature)

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
