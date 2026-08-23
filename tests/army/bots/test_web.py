"""The page, which is a renderer and never a second authorisation route.

Two things matter here and neither is layout. A verdict from the page goes
through exactly the same bound path as one from the CLI, so a stale answer is
refused identically. And everything the page shows came from a bot, so none of
it may execute.
"""

from __future__ import annotations

import pytest

from army.bots.approvals import ITERATION_GATE, ApprovalState, ApprovalStore
from army.bots.messages import MessageKind, MessageStore
from army.bots.model import BotStatus, WakeKind, WakePolicy
from army.bots.store import BotStore
from army.bots.web import BotsSite
from army.state import CommandKind
from army.store import Store
from tests.army.bots.conftest import activate, make_bot

NOW = 1_700_000_000
HEARTBEAT = "army.bots.workloads.heartbeat:HeartbeatWorkload"


@pytest.fixture()
def site(store: Store, bots: BotStore) -> BotsSite:
    return BotsSite(store, bots, ApprovalStore(bots), MessageStore(bots))


def _ask(site: BotsSite, bot_id: str, run_id: str = "run", **overrides: object) -> object:
    params: dict = {
        "bot_id": bot_id,
        "run_id": run_id,
        "run_version": 1,
        "verb": ITERATION_GATE,
        "parameters": {"iteration": 1},
        "question": "Merge?",
        "options": ["merge", "iterate"],
        "now": NOW,
    }
    params.update(overrides)  # type: ignore[arg-type]
    return site.approvals.request(**params)  # type: ignore[arg-type]


def test_the_roster_renders_with_no_bots_at_all(site: BotsSite) -> None:
    """A first run must say what to do next, not show an empty page."""
    page = site.index(now=NOW)
    assert "No bots yet" in page
    assert "army bots example" in page
    assert "Nothing is waiting on you" in page


def test_what_needs_a_person_comes_before_the_fleet(site: BotsSite, bots: BotStore) -> None:
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    _ask(site, bot.id)
    page = site.index(now=NOW)
    assert page.index("Needs you") < page.index("Fleet")
    assert "Merge?" in page


def test_a_verdict_from_the_page_takes_the_same_bound_path(
    site: BotsSite, bots: BotStore, store: Store
) -> None:
    """The page is a renderer. The binding is not re-implemented behind it."""
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    request = _ask(site, bot.id)

    notice, problem = site.verdict(
        {"approval": [request.id], "choice": ["merge"]},  # type: ignore[attr-defined]
        now=NOW,
    )
    assert problem == ""
    assert "approve" in notice

    command = store.next_command("run")
    assert command is not None and command.kind is CommandKind.APPROVE
    assert command.payload == {"choice": "merge"}

    reread = site.approvals.get(request.id)  # type: ignore[attr-defined]
    assert reread is not None and reread.state is ApprovalState.APPROVED
    assert reread.decided_by == "human:web"


def test_answering_the_same_question_twice_is_refused(site: BotsSite, bots: BotStore) -> None:
    """A refresh after a post must not re-answer it."""
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    request = _ask(site, bot.id)
    form = {"approval": [request.id], "choice": ["merge"]}  # type: ignore[attr-defined]

    site.verdict(form, now=NOW)
    _notice, problem = site.verdict(form, now=NOW)
    assert "not open" in problem


def test_an_owner_only_question_cannot_be_answered_from_the_page(
    site: BotsSite, bots: BotStore
) -> None:
    """A click in a browser never satisfies spend, execute_order or add_dependency."""
    bot = activate(bots, make_bot("treasurer", workload=HEARTBEAT), now=NOW)
    request = _ask(site, bot.id, verb="spend", parameters={"amount": 40}, options=["pay"])

    page = site.index(now=NOW)
    assert "owner-only" in page

    _notice, problem = site.verdict(
        {"approval": [request.id], "choice": ["pay"]},  # type: ignore[attr-defined]
        now=NOW,
    )
    assert "owner" in problem
    reread = site.approvals.get(request.id)  # type: ignore[attr-defined]
    assert reread is not None and reread.state is ApprovalState.PENDING


def test_a_verdict_records_itself_in_the_channel(site: BotsSite, bots: BotStore) -> None:
    """So the decision sits next to the question, whoever answered it."""
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    request = _ask(site, bot.id)
    site.verdict(
        {"approval": [request.id], "choice": ["merge"]},  # type: ignore[attr-defined]
        now=NOW,
    )
    verdicts = [
        message
        for message in site.messages.channel(bot.id)
        if message.kind is MessageKind.VERDICT
    ]
    assert len(verdicts) == 1
    assert verdicts[0].author == "human:web"


def test_everything_a_bot_wrote_is_escaped_before_it_reaches_the_page(
    site: BotsSite, bots: BotStore
) -> None:
    """The page renders bot output, and a bot is not a trusted author.

    A mission, a question, or a pause reason is text a model produced — or that
    a page a bot was reading produced. None of it may become markup.
    """
    hostile = '<script>alert("x")</script>'
    bot = make_bot("scout", workload=HEARTBEAT, wake=WakePolicy(kind=WakeKind.MANUAL))
    bot.mission = hostile
    activate(bots, bot, now=NOW)
    _ask(site, bot.id, question=hostile, options=[hostile])

    page = site.index(now=NOW, notice=hostile, problem=hostile)
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_the_page_forbids_script_execution_outright(site: BotsSite) -> None:
    """Escaping is the fix; the policy header is the seatbelt."""
    from army.bots.web import _Handler

    sent: dict[str, str] = {}

    class Recorder(_Handler):
        def __init__(self) -> None:
            pass

        def send_response(self, code: int, message: str | None = None) -> None:
            sent["status"] = str(code)

        def send_header(self, keyword: str, value: str) -> None:
            sent[keyword] = value

        def end_headers(self) -> None:
            return None

        @property
        def wfile(self):  # type: ignore[no-untyped-def]
            class Sink:
                def write(self, _data: bytes) -> None:
                    return None

            return Sink()

    Recorder()._send(200, "<p>hi</p>", "text/html")
    assert "default-src 'none'" in sent["Content-Security-Policy"]
    assert sent["X-Content-Type-Options"] == "nosniff"


def test_the_json_view_says_what_the_roster_says(site: BotsSite, bots: BotStore) -> None:
    """For anything that is not a browser — a phone shortcut, a status bar."""
    import json

    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    bots.set_status(bot, BotStatus.PAUSED, now=NOW, reason="a streak ran out")
    _ask(site, bot.id)

    data = json.loads(site.api(now=NOW))
    assert data["bots"][0]["slug"] == "scout"
    assert data["bots"][0]["paused_reason"] == "a streak ran out"
    assert data["pending"][0]["question"] == "Merge?"


def test_a_paused_bot_says_why_on_the_page(site: BotsSite, bots: BotStore) -> None:
    """The reason is the whole point of recording it."""
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    bots.set_status(bot, BotStatus.PAUSED, now=NOW, reason="its workload could not be loaded")
    assert "its workload could not be loaded" in site.index(now=NOW)
