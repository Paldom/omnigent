"""Answering from a chat link, without handing the chat room the fleet."""

from __future__ import annotations

import pytest

from army.bots.approvals import ApprovalStore
from army.bots.messages import MessageStore
from army.bots.store import BotStore
from army.bots.web import BotsSite
from army.egress import mint
from army.state import Run, RunState
from army.store import Store
from tests.army.bots.conftest import activate, make_bot

NOW = 1_700_000_000
TOKEN = "a-page-token"
SECRET = "a-link-key"


@pytest.fixture()
def site(store: Store, bots: BotStore) -> BotsSite:
    messages = MessageStore(bots)
    return BotsSite(
        store,
        bots,
        ApprovalStore(bots, messages=messages),
        messages,
        TOKEN,
        link_secret=SECRET,
    )


_made = 0


def _waiting(site: BotsSite, bots: BotStore) -> str:
    """A bot parked on a real question, the way the loop parks one."""
    global _made
    _made += 1
    bot = activate(bots, make_bot(f"scout{_made}"), now=NOW)
    run = site.store.create_run(Run.new("w", {}, now=NOW, bot_id=bot.id))
    run = site.store.transition(run, RunState.DISPATCHING, now=NOW)
    run = site.store.transition(run, RunState.COLLECTING, now=NOW)
    run = site.store.transition(run, RunState.EVALUATING, now=NOW)
    run = site.store.transition(run, RunState.WAITING_HUMAN, now=NOW)
    request = site.approvals.request(
        bot_id=bot.id,
        run_id=run.id,
        run_version=run.version,
        verb="iteration_gate",
        parameters={"iteration": 1},
        question="Ship the candidate?",
        options=["continue", "stop"],
        evidence={},
        thread_id=run.id,
        now=NOW,
    )
    return request.id


def test_a_link_preview_crawler_cannot_approve(site: BotsSite, bots: BotStore) -> None:
    """Chat clients GET every URL they unfurl.

    So a link that decided on GET would be decided by the preview bot, seconds
    after posting, from inside the network, with no person involved. That is
    the trap this whole route exists to avoid.
    """
    approval_id = _waiting(site, bots)
    token = mint(SECRET, approval_id, now=NOW)

    status, body, _ = site.confirm(token, now=NOW)

    assert status == 200
    assert "Ship the candidate?" in body
    assert site.approvals.get(approval_id).state.name == "PENDING", "GET decided nothing"


def test_the_link_answers_that_question_and_no_other(site: BotsSite, bots: BotStore) -> None:
    """Pasting the page's own token into a chat room hands the room the fleet."""
    first = _waiting(site, bots)
    second = _waiting(site, bots)
    token = mint(SECRET, first, now=NOW)

    notice, problem = site.decide_by_link({"t": [token], "choice": ["continue"]}, now=NOW)

    assert problem == "", notice
    assert site.approvals.get(first).state.name == "APPROVED"
    assert site.approvals.get(second).state.name == "PENDING"


def test_a_forged_link_decides_nothing(site: BotsSite, bots: BotStore) -> None:
    approval_id = _waiting(site, bots)

    notice, problem = site.decide_by_link(
        {"t": [mint("another-key", approval_id, now=NOW)], "choice": ["continue"]},
        now=NOW,
    )

    assert notice == ""
    assert "not signed" in problem
    assert site.approvals.get(approval_id).state.name == "PENDING"


def test_an_answered_question_says_so_rather_than_erroring(site: BotsSite, bots: BotStore) -> None:
    """The commonest second click on a chat link is somebody else's."""
    approval_id = _waiting(site, bots)
    token = mint(SECRET, approval_id, now=NOW)
    site.decide_by_link({"t": [token], "choice": ["continue"]}, now=NOW)

    status, body, _ = site.confirm(token, now=NOW)

    assert status == 200
    assert "already been answered" in body


def test_a_link_goes_through_the_same_path_the_page_uses(site: BotsSite, bots: BotStore) -> None:
    """Or the bindings, the audit trail and the successor wake drift from it."""
    approval_id = _waiting(site, bots)
    site.decide_by_link(
        {"t": [mint(SECRET, approval_id, now=NOW)], "choice": ["continue"]}, now=NOW
    )

    decided = site.approvals.get(approval_id)
    assert decided.state.name == "APPROVED"
    assert decided.decided_by, "the audit trail records who"
