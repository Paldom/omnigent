"""The page, which is a renderer and never a second authorisation route.

Two things matter here and neither is layout. A verdict from the page goes
through exactly the same bound path as one from the CLI, so a stale answer is
refused identically. And everything the page shows came from a bot, so none of
it may execute.
"""

from __future__ import annotations

import pytest

from army.bots.approvals import (
    ITERATION_GATE,
    ApprovalRefused,
    ApprovalState,
    ApprovalStore,
)
from army.bots.messages import MessageKind, MessageStore
from army.bots.model import BotStatus, WakeKind, WakePolicy
from army.bots.store import BotStore
from army.bots.web import BotsSite
from army.state import CommandKind, Run
from army.store import Store
from tests.army.bots.conftest import activate, make_bot

NOW = 1_700_000_000
HEARTBEAT = "army.bots.workloads.heartbeat:HeartbeatWorkload"


TOKEN = "a-test-token"


@pytest.fixture()
def site(store: Store, bots: BotStore) -> BotsSite:
    return BotsSite(store, bots, ApprovalStore(bots), MessageStore(bots), TOKEN)


def _ask(site: BotsSite, bot_id: str, **overrides: object) -> object:
    """Park a real run on a question, so the bindings have something to check."""
    run = site.store.create_run(Run.new("w", {}, now=NOW, bot_id=bot_id))
    params: dict = {
        "bot_id": bot_id,
        "run_id": run.id,
        "run_version": run.version,
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
        {
            "approval": [request.id],  # type: ignore[attr-defined]
            "choice": ["merge"],
            "decision": ["approve"],
        },
        now=NOW,
    )
    assert problem == ""
    assert "approve" in notice

    command = store.next_command(request.run_id)  # type: ignore[attr-defined]
    assert command is not None and command.kind is CommandKind.APPROVE
    assert command.payload == {"choice": "merge"}

    reread = site.approvals.get(request.id)  # type: ignore[attr-defined]
    assert reread is not None and reread.state is ApprovalState.APPROVED
    assert reread.decided_by == "web:token"


def test_answering_the_same_question_twice_is_refused(site: BotsSite, bots: BotStore) -> None:
    """A refresh after a post must not re-answer it."""
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    request = _ask(site, bot.id)
    form = {
        "approval": [request.id],  # type: ignore[attr-defined]
        "choice": ["merge"],
        "decision": ["approve"],
    }

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
        {
            "approval": [request.id],  # type: ignore[attr-defined]
            "choice": ["pay"],
            "decision": ["approve"],
        },
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
        {
            "approval": [request.id],  # type: ignore[attr-defined]
            "choice": ["merge"],
            "decision": ["approve"],
        },
        now=NOW,
    )
    verdicts = [
        message for message in site.messages.channel(bot.id) if message.kind is MessageKind.VERDICT
    ]
    assert len(verdicts) == 1
    assert verdicts[0].author == "web:token"


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


# ── authorisation ─────────────────────────────────────────────────
#
# The finding that mattered: bots run on this box with network access, so
# "only the tailnet can reach it" meant every bot could read the pending
# approvals and answer its own — a complete bypass of the approval machinery,
# by the thing it exists to gate, recorded as a human's decision.


def test_the_token_lives_where_bots_cannot_read_it(tmp_path) -> None:
    """The control-plane directory is already withheld from every sandbox.

    That is the whole argument: the one file that authorises a decision is the
    one file a bot has no read path to.
    """
    from army.bots.isolation import CONTROL_PLANE_DIRS
    from army.bots.web import TOKEN_FILE

    assert any(str(TOKEN_FILE).endswith(f"{d}/web-token") for d in CONTROL_PLANE_DIRS), (
        f"{TOKEN_FILE} is not inside a directory the sandbox denies: {CONTROL_PLANE_DIRS}"
    )


def test_the_token_is_minted_once_and_kept_private(tmp_path) -> None:
    from army.bots.web import read_or_mint_token

    path = tmp_path / "army" / "web-token"
    first = read_or_mint_token(path)
    assert len(first) >= 32
    assert read_or_mint_token(path) == first, "a second call minted a different secret"
    assert path.stat().st_mode & 0o077 == 0, "the token is readable by other users"


def test_a_request_without_the_token_is_refused(site: BotsSite) -> None:
    assert site.authorises("") is False
    assert site.authorises("guess") is False
    assert site.authorises(TOKEN) is True


def test_a_missing_decision_is_not_an_approval(site: BotsSite, bots: BotStore) -> None:
    """`choice != "__deny__"` meant a POST with no fields at all approved.

    A bot authors its own option labels, so the primary button could read
    "Deny this request" and still approve. The decision is a separate field
    the page controls.
    """
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    request = _ask(site, bot.id)

    _notice, problem = site.verdict({"approval": [request.id]}, now=NOW)  # type: ignore[attr-defined]
    assert problem, "an empty form was accepted"
    reread = site.approvals.get(request.id)  # type: ignore[attr-defined]
    assert reread is not None and reread.state is ApprovalState.PENDING


def test_a_wildcard_cannot_stand_in_for_an_id(site: BotsSite, bots: BotStore) -> None:
    """`LIKE` matching let a caller send `00%` and have the store choose.

    Spraying two hex characters at a time is enough to deny approvals you were
    never shown the ids of.
    """
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    request = _ask(site, bot.id)
    prefix = request.id[:2] + "%"  # type: ignore[attr-defined]

    _notice, problem = site.verdict({"approval": [prefix], "decision": ["deny"]}, now=NOW)
    assert "not an approval id" in problem
    reread = site.approvals.get(request.id)  # type: ignore[attr-defined]
    assert reread is not None and reread.state is ApprovalState.PENDING


def test_a_verdict_is_not_attributed_to_a_human_the_page_never_saw(
    site: BotsSite, bots: BotStore
) -> None:
    """The token proves someone read a file, not who they are."""
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    request = _ask(site, bot.id)
    site.verdict(
        {"approval": [request.id], "choice": ["merge"], "decision": ["approve"]},  # type: ignore[attr-defined]
        now=NOW,
    )
    reread = site.approvals.get(request.id)  # type: ignore[attr-defined]
    assert reread is not None
    assert not reread.decided_by.startswith("human:")


def test_a_missing_run_fails_the_binding_rather_than_skipping_it(
    site: BotsSite, bots: BotStore
) -> None:
    """Passing None skipped the check. A lookup miss must fail closed."""
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    request = site.approvals.request(
        bot_id=bot.id,
        run_id="a-run-that-does-not-exist",
        run_version=1,
        verb=ITERATION_GATE,
        parameters={},
        question="?",
        options=["yes"],
        now=NOW,
    )
    _notice, problem = site.verdict(
        {"approval": [request.id], "choice": ["yes"], "decision": ["approve"]},
        now=NOW,
    )
    assert "run moved" in problem


def test_the_page_carries_the_token_in_its_own_form(site: BotsSite, bots: BotStore) -> None:
    """So the operator's click works without them pasting anything."""
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    _ask(site, bot.id)
    page = site.index(now=NOW)
    assert f'value="{TOKEN}"' in page, "the operator would have to paste the token by hand"
    # Approve is carried by the button's formaction, deny by its own value, so
    # neither is the default and the option label cannot decide which happens.
    assert 'formaction="/verdict?decision=approve"' in page
    assert 'name="decision" value="deny"' in page


def test_the_card_shows_what_the_verdict_binds_not_just_the_prose(
    site: BotsSite, bots: BotStore
) -> None:
    """The question is the bot's marketing; the evidence is the operation.

    A human bound to a hash of arguments they were never shown is not bound to
    anything — "spend $5 on a domain" can carry a five-figure transfer and the
    digest still matches.
    """
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    request = _ask(
        site,
        bot.id,
        question="Just a small charge?",
        evidence={"amount": "500000", "to": "attacker"},
    )
    page = site.index(now=NOW)
    assert "500000" in page, "the operator cannot see what they are approving"
    assert "attacker" in page
    assert request.action_hash[:12] in page  # type: ignore[attr-defined]


def test_an_owner_only_question_offers_no_approve_button(site: BotsSite, bots: BotStore) -> None:
    """A button guaranteed to be refused teaches that the buttons are advisory."""
    bot = activate(bots, make_bot("treasurer", workload=HEARTBEAT), now=NOW)
    _ask(site, bot.id, verb="spend", parameters={"amount": 40}, options=["pay"])
    page = site.index(now=NOW)
    assert "owner-only" in page
    assert 'formaction="/verdict?decision=approve"' not in page
    assert 'name="decision" value="deny"' in page


def test_an_approval_with_nothing_to_choose_is_refused_at_ask_time(
    site: BotsSite, bots: BotStore
) -> None:
    """An empty option list is satisfied by the empty string an empty form sends."""
    bot = activate(bots, make_bot("scout", workload=HEARTBEAT), now=NOW)
    with pytest.raises(ApprovalRefused, match="at least one option"):
        _ask(site, bot.id, options=[])


def test_a_fabricated_request_object_cannot_downgrade_an_owner_verb(
    site: BotsSite, bots: BotStore
) -> None:
    """`decide` used to trust the dataclass it was handed.

    The CAS fences only (id, version, state), so a never-updated row accepted a
    caller-built object claiming `requires_owner=False`.
    """
    import dataclasses

    bot = activate(bots, make_bot("treasurer", workload=HEARTBEAT), now=NOW)
    real = _ask(site, bot.id, verb="spend", parameters={"amount": 40}, options=["pay"])
    forged = dataclasses.replace(real, requires_owner=False)  # type: ignore[type-var]

    with pytest.raises(ApprovalRefused, match="owner"):
        site.approvals.decide(forged, approved=True, decided_by="x", now=NOW, choice="pay")
