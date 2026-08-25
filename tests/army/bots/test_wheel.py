"""Control handoff: while a person is driving, the bot's actions are refused.

OpenBot's rule, kept verbatim, because the tempting alternative is worse. A
queued click lands after the human has navigated away, on a page that is no
longer the one it was reasoned about; a refusal with a reason is something an
agent can handle.

The wheel shipped once with no tests, and two things were wrong at the same
time: the store was constructed but never handed to the site, and the
session-to-bot join read stale run rows. Both looked exactly like "nobody is
driving", which is the failure this whole file exists to catch.
"""

from __future__ import annotations

import pytest

from army.bots.approvals import ApprovalStore
from army.bots.messages import MessageStore
from army.bots.store import BotStore
from army.bots.web import BotsSite
from army.bots.wheel import DEFAULT_LEASE_S, HANDBACK, REFUSAL, WheelStore
from army.store import Store
from tests.army.bots.conftest import activate, make_bot

NOW = 1_700_000_000
TOKEN = "a-test-token"
PROFILE = "persist:bot-watcher"


@pytest.fixture()
def wheels(bots: BotStore) -> WheelStore:
    return WheelStore(bots)


@pytest.fixture()
def site(store: Store, bots: BotStore, wheels: WheelStore) -> BotsSite:
    """A site with a wheel, which is the configuration ``army bots serve`` builds.

    Constructing one without is what shipped: every take succeeded, every
    refusal came back empty, and the page said the handoff had happened.
    """
    return BotsSite(store, bots, ApprovalStore(bots), MessageStore(bots), TOKEN, wheels=wheels)


def _watcher(bots: BotStore, slug: str = "watcher", profile: str = PROFILE) -> str:
    """A bot with a browser of its own — which is every bot, in a real fleet."""
    bot = activate(bots, make_bot(slug, browser_profile=profile), now=NOW)
    return bot.id


def test_a_bot_drives_its_own_browser_by_default(site: BotsSite, bots: BotStore) -> None:
    """The bot driving is the absence of a row, so a lost write fails towards work."""
    _watcher(bots)
    assert site.wheel_for_profile(PROFILE, now=NOW) == ""


def test_taking_the_wheel_refuses_the_bot(site: BotsSite, bots: BotStore) -> None:
    """The whole point: a held wheel is a refusal the agent can read."""
    _watcher(bots)
    _, problem = site.wheel({"bot": ["watcher"], "action": ["take"]}, now=NOW)

    assert problem == ""
    assert site.wheel_for_profile(PROFILE, now=NOW) == REFUSAL


def test_the_refusal_tells_the_agent_not_to_retry(site: BotsSite, bots: BotStore) -> None:
    """A refusal an agent answers with a retry loop is a busy-wait with extra steps."""
    _watcher(bots)
    site.wheel({"bot": ["watcher"], "action": ["take"]}, now=NOW)

    refusal = site.wheel_for_profile(PROFILE, now=NOW)
    assert "refused" in refusal
    assert "not queued" in refusal
    assert "retry" in refusal


def test_releasing_hands_the_browser_back(site: BotsSite, bots: BotStore) -> None:
    _watcher(bots)
    site.wheel({"bot": ["watcher"], "action": ["take"]}, now=NOW)
    site.wheel({"bot": ["watcher"], "action": ["release"]}, now=NOW)

    assert site.wheel_for_profile(PROFILE, now=NOW) == ""


def test_a_lapsed_hold_does_not_hand_the_wheel_back(site: BotsSite, bots: BotStore) -> None:
    """The lease used to return the wheel on its own. That trade was backwards.

    It was insurance against a closed laptop bricking a bot. But the moment a
    hold lapses is exactly the moment the person may be mid-login, and the
    bot's first act would be to snapshot the form they are typing into —
    silently. A bot waiting on a hand-back is a visible stop; a bot resuming on
    a live session is the one failure nobody forgives.
    """
    _watcher(bots)
    site.wheel({"bot": ["watcher"], "action": ["take"]}, now=NOW)

    assert site.wheel_for_profile(PROFILE, now=NOW + DEFAULT_LEASE_S - 1) == REFUSAL
    # Still refused, and with a different sentence: "wait, somebody is driving"
    # and "nobody is driving and nobody has said you may" are different
    # situations, and only the second needs a person to clear it.
    lapsed = site.wheel_for_profile(PROFILE, now=NOW + DEFAULT_LEASE_S + 1)
    assert lapsed == HANDBACK
    assert "waiting on a hand-back" in lapsed

    site.wheel({"bot": ["watcher"], "action": ["release"]}, now=NOW + DEFAULT_LEASE_S + 2)
    assert site.wheel_for_profile(PROFILE, now=NOW + DEFAULT_LEASE_S + 3) == ""


def test_one_persons_wheel_does_not_refuse_another_bot(site: BotsSite, bots: BotStore) -> None:
    """Nine bots, nine browsers. Taking one must not stop the other eight."""
    _watcher(bots)
    _watcher(bots, slug="other", profile="persist:bot-other")
    site.wheel({"bot": ["watcher"], "action": ["take"]}, now=NOW)

    assert site.wheel_for_profile("persist:bot-other", now=NOW) == ""


def test_the_handoff_is_announced_in_the_channel(site: BotsSite, bots: BotStore) -> None:
    """A refusal is a mysterious gap in an iteration unless both ends are recorded."""
    bot_id = _watcher(bots)
    messages = MessageStore(bots)
    site.wheel({"bot": ["watcher"], "action": ["take"], "why": ["checking the VIP tier"]}, now=NOW)
    site.wheel({"bot": ["watcher"], "action": ["release"]}, now=NOW + 60)

    said = [message.body for message in messages.channel(bot_id, limit=10)]
    assert any("took the wheel" in body for body in said)
    assert any("handed back" in body for body in said)


def test_a_site_without_a_wheel_says_so_rather_than_pretending(
    store: Store, bots: BotStore
) -> None:
    """The failure that shipped: a take that reports success and refuses nothing."""
    site = BotsSite(store, bots, ApprovalStore(bots), MessageStore(bots), TOKEN)
    _watcher(bots)

    _, problem = site.wheel({"bot": ["watcher"], "action": ["take"]}, now=NOW)
    assert problem, "a take that cannot be honoured must not report success"


def test_an_unknown_profile_fails_open(site: BotsSite, bots: BotStore) -> None:
    """A control-plane hiccup must not freeze every bot's browser."""
    _watcher(bots)
    site.wheel({"bot": ["watcher"], "action": ["take"]}, now=NOW)

    assert site.wheel_for_profile("persist:bot-nobody", now=NOW) == ""
    assert site.wheel_for_profile("", now=NOW) == ""


@pytest.mark.parametrize(
    "held_as,asked_as",
    [
        ("persist:bot-watcher", "bot-watcher"),
        ("bot-watcher", "persist:bot-watcher"),
    ],
)
def test_either_spelling_of_a_profile_still_refuses(
    store: Store, bots: BotStore, held_as: str, asked_as: str
) -> None:
    """One browser, two spellings, and a refusal that must not depend on which.

    Definitions write the Electron partition form and a hand-written one often
    writes the bare slug. Compared literally, a held wheel refuses nothing —
    indistinguishable from no wheel at all.
    """
    site = BotsSite(
        store, bots, ApprovalStore(bots), MessageStore(bots), TOKEN, wheels=WheelStore(bots)
    )
    _watcher(bots, profile=held_as)
    site.wheel({"bot": ["watcher"], "action": ["take"]}, now=NOW)

    assert site.wheel_for_profile(asked_as, now=NOW) == REFUSAL


def test_the_wheel_and_the_gateway_agree_on_what_a_profile_is_called() -> None:
    """Two canonicalisers that disagree is a wheel that refuses nothing.

    The gateway keys browsers one way and the wheel keyed holds another, so
    ``persist:acme/prod`` and ``persist:acme_prod`` were one Chromium and two
    identities: a person took the wheel of one and the other kept driving the
    page they were typing into, with nothing logged.

    The rule is duplicated because the server imports nothing from this
    package. This is what keeps the copies honest.
    """
    from army.bots.wheel import canonical_profile
    from omnigent.browser.gateway import _canonical

    for name in (
        "persist:bot-kraken-fee-watch",
        "bot-kraken-fee-watch",
        "Bot-A",
        "bot-a",
        "acme/prod",
        "acme_prod",
        "acme prod",
        "acme:prod",
        "persist:..",
        "..",
        "",
        "   ",
        "bot-а",  # Cyrillic a — a lookalike identity
        "x" * 200,
    ):
        assert canonical_profile(name) == _canonical(name), name


def test_releasing_records_whose_hold_it_was(site: BotsSite, bots: BotStore) -> None:
    """Two people watching one bot is a normal Tuesday, so a release ends somebody's turn.

    There is one operator identity here, so this cannot be an authorisation
    check. It can be a record — which is what makes "the browser stopped being
    mine halfway through signing in" explicable rather than mysterious.
    """
    bot_id = _watcher(bots)
    messages = MessageStore(bots)
    site.wheel({"bot": ["watcher"], "action": ["take"], "why": ["signing in"]}, now=NOW)
    site.wheel({"bot": ["watcher"], "action": ["release"]}, now=NOW + 60)

    handback = next(
        message for message in messages.channel(bot_id, limit=10) if "handed back" in message.body
    )
    assert "s of the hold left" in handback.body


def test_releasing_a_wheel_nobody_holds_says_so(site: BotsSite, bots: BotStore) -> None:
    """Rather than announcing a handback that did not happen."""
    _watcher(bots)
    notice, problem = site.wheel({"bot": ["watcher"], "action": ["release"]}, now=NOW)

    assert problem == ""
    assert "already driving itself" in notice
