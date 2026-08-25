"""Telling a person where they already are, without handing the room the fleet.

The whole point of a nudge is that it carries no authority. Every test here is
about a way that could quietly stop being true.
"""

from __future__ import annotations

import pytest

from army.egress import LINK_TTL_S, Egress, LinkRefused, approval_in, mint

NOW = 1_700_000_000
SECRET = "a-signing-key"
APPROVAL = "a" * 32


def test_a_link_answers_exactly_one_approval() -> None:
    """The page's own token authorises answering *everything*.

    Pasting that into a chat room would hand the room the fleet, so a link is
    scoped to the one question it was sent about.
    """
    token = mint(SECRET, APPROVAL, now=NOW)

    assert approval_in(SECRET, token, now=NOW) == APPROVAL
    assert APPROVAL not in token.split(".")[1], "the signature is not the payload"


def test_a_forged_link_is_refused() -> None:
    token = mint(SECRET, APPROVAL, now=NOW)
    tampered = mint("another-key", APPROVAL, now=NOW)

    with pytest.raises(LinkRefused, match="not signed"):
        approval_in(SECRET, tampered, now=NOW)
    with pytest.raises(LinkRefused, match="not signed"):
        approval_in(SECRET, token[:-1] + ("0" if token[-1] != "0" else "1"), now=NOW)


def test_a_link_expires() -> None:
    """A message in a channel somebody left open for a month is not a key."""
    token = mint(SECRET, APPROVAL, now=NOW)

    assert approval_in(SECRET, token, now=NOW + LINK_TTL_S - 1) == APPROVAL
    with pytest.raises(LinkRefused, match="expired"):
        approval_in(SECRET, token, now=NOW + LINK_TTL_S)


@pytest.mark.parametrize("token", ["", "junk", "junk.", ".sig", "!!!.abc"])
def test_a_malformed_link_is_refused_rather_than_crashing(token: str) -> None:
    with pytest.raises(LinkRefused):
        approval_in(SECRET, token, now=NOW)


def test_a_control_plane_that_signs_nothing_accepts_nothing() -> None:
    """An unset secret must not verify everything by accident."""
    with pytest.raises(LinkRefused, match="signs no links"):
        approval_in("", mint(SECRET, APPROVAL, now=NOW), now=NOW)


def test_nothing_is_sent_when_no_webhook_is_configured() -> None:
    """Absent configuration is the off switch, not a broken send."""
    assert Egress().enabled() is False
    Egress().notify(bot="scout", question="q", evidence={}, link="")


def test_a_link_needs_both_a_secret_and_somewhere_to_point() -> None:
    """A link to 127.0.0.1 in a chat room helps nobody."""
    assert Egress(webhook_url="http://x", secret=SECRET).link_for(APPROVAL, now=NOW) == ""
    assert (
        Egress(webhook_url="http://x", public_url="http://tail").link_for(APPROVAL, now=NOW) == ""
    )

    linked = Egress(webhook_url="http://x", secret=SECRET, public_url="http://tail/")
    assert linked.link_for(APPROVAL, now=NOW).startswith("http://tail/approve?t=")


def test_a_provider_having_a_bad_afternoon_cannot_fail_an_iteration() -> None:
    """The approval row is the truth; this is a courtesy on the tick's path.

    A nudge that can take the fleet down is worse than no nudge.
    """
    unreachable = Egress(webhook_url="http://127.0.0.1:1/nope", secret=SECRET)

    unreachable.notify(bot="scout", question="q", evidence={"a": "b"}, link="")


def test_evidence_is_bounded_before_it_reaches_a_chat_room(monkeypatch) -> None:
    """An evidence blob can carry a whole diff. A chat message is not that."""
    sent: dict = {}

    class _Response:
        def read(self) -> bytes:
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def _fake(request, timeout):
        sent["body"] = request.data.decode()
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    Egress(webhook_url="http://x").notify(
        bot="scout",
        question="ship it?",
        evidence={f"k{n}": "v" * 5_000 for n in range(30)},
        link="http://tail/approve?t=abc",
    )

    assert len(sent["body"]) < 4_000
    assert "ship it?" in sent["body"]
