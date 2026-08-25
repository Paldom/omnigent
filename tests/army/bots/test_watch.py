"""The watcher's first live run, and the three ways it lied about itself.

One run of ``kraken-fee-watch`` produced a correct, well-sourced answer and
recorded ``completed · nothing to report``. Three separate defects had to line
up for that, and none of them raised: a wrong accessor, a wrong reader, and a
parser stricter than the thing it parses. Each is pinned here by its failure.
"""

from __future__ import annotations

import json
from typing import Any

from army.bots.workloads.watch import WatchWorkload
from army.state import Run, RunState
from tests.army.bots.conftest import FakeOmni

NOW = 1_700_000_000

#: What the agent actually wrote on that run, verbatim in shape.
REAL_REPLY = (
    "**CHANGED** (first look — baseline recorded)\n\n"
    "**Spot Tier 1 (lowest volume): maker 0.40% / taker 0.80% per side.**"
)


def _workload(tmp_path: Any, **config: Any) -> WatchWorkload:
    return WatchWorkload(
        url="https://example.com/fees",
        question="what is the fee",
        workspace=str(tmp_path),
        **config,
    )


def _run(*, outstanding: list[str] | None = None, **artifacts: Any) -> Run:
    return Run(
        id="0bb51cd1c8ad0000",
        workflow="army.bots.workloads.watch:WatchWorkload",
        state=RunState.EVALUATING,
        version=1,
        attempt=1,
        created_at=NOW,
        updated_at=NOW,
        payload={},
        artifacts=dict(artifacts),
        outstanding=list(outstanding or []),
        bot_id="bot",
    )


def test_collect_reads_what_the_agent_said_not_what_a_human_typed(tmp_path: Any) -> None:
    """The reply came back empty because ``replies_after`` skips the assistant.

    That helper exists to read a person's answer to a question and deliberately
    ignores assistant messages, so an agent that worked perfectly collected as
    ``""`` — and an empty reply is indistinguishable from an agent that said
    nothing at all.
    """
    omni = FakeOmni()
    omni.agent_replies = [REAL_REPLY]
    omni.replies = ["a human typing something unrelated"]

    done, artifacts = _workload(tmp_path).collect(_run(outstanding=["conv_0"]), omni)

    assert done is True
    assert "maker 0.40%" in artifacts["reply"]
    assert "unrelated" not in artifacts["reply"]


def test_the_verdict_comes_from_the_answer_not_the_narration(tmp_path: Any) -> None:
    """An agent narrates before it works, and the narration is not the verdict.

    A live run collected "I'll load the browser tools and check the page." as
    line one, matched neither CHANGED nor UNCHANGED, and reported nothing to
    report — about a run that had found the number and quoted its row.
    """
    omni = FakeOmni()
    omni.agent_replies = [
        "I'll load the browser tools and check the page.",
        "Page loaded. Taking a snapshot.",
        REAL_REPLY,
    ]

    _, artifacts = _workload(tmp_path).collect(_run(outstanding=["conv_0"]), omni)
    question, _, evidence = _workload(tmp_path).evaluate(_run(reply=artifacts["reply"]))

    assert question, "the verdict is in the last message"
    assert "CHANGED" in evidence["verdict"]
    assert "browser tools" not in evidence["verdict"]


def test_a_bolded_verdict_still_counts_as_changed(tmp_path: Any) -> None:
    """``**CHANGED**`` was read as "the agent did not say".

    The brief asks for the word on its own line; models deliver it wearing
    markdown. Matching the raw line meant the one run that found real news
    reported none.
    """
    question, options, evidence = _workload(tmp_path).evaluate(_run(reply=REAL_REPLY))

    assert question, "a bolded CHANGED must still raise the question"
    assert options == ["acknowledge", "stop"]
    assert "CHANGED" in evidence["verdict"]


def test_unchanged_is_not_mistaken_for_changed(tmp_path: Any) -> None:
    """The normaliser must not let ``UNCHANGED`` match ``CHANGED``.

    Stripping punctuation to be lenient is exactly how a watcher starts paging
    somebody every morning about a page that never moves.
    """
    for reply in ("UNCHANGED", "**UNCHANGED**", "### Unchanged — same as yesterday"):
        question, options, _ = _workload(tmp_path).evaluate(_run(reply=reply))
        assert question == "", f"{reply!r} must not raise a question"
        assert options == []


def test_the_workload_does_not_name_the_browser(tmp_path: Any) -> None:
    """Two places to name a bot's browser is one place to get it wrong.

    The watcher labelled its own sessions and the research workload did not, so
    nine of ten bots in the crypto example could not browse at all and nothing
    said so. The supervisor labels every session now; a workload that also did
    it would be the fork.
    """
    seen: dict[str, Any] = {}

    class Recording(FakeOmni):
        def create_session(self, agent_id: str, **kwargs: Any) -> str:
            seen.update(kwargs)
            return super().create_session(agent_id, **kwargs)

    _workload(tmp_path, agent="claude-native-ui").dispatch(_run(), Recording())

    assert "labels" not in seen


def test_a_login_wall_asks_for_a_person_rather_than_a_credential(tmp_path: Any) -> None:
    """The escalation a shared browser exists for.

    A bot that meets a sign-in page has exactly two options worth having: stop,
    or ask a person to take the wheel of *this* browser and sign in. Anything
    that involves the bot obtaining a credential is the wrong one — and a
    screenshot taken beside an agent's own fetching could not offer the right
    one, because the person would be signing in to a different browser.
    """
    reply = "LOGIN\n\nKraken wants an email and password at /sign-in before it shows VIP tiers."
    question, options, evidence = _workload(tmp_path).evaluate(_run(reply=reply))

    assert "sign-in" in question
    assert options == ["I signed in — look again", "stop"]
    assert evidence["needs"] == "somebody to sign in on this browser"


def test_a_login_wall_records_no_baseline(tmp_path: Any) -> None:
    """Nothing was read, so there is nothing to compare against next time.

    Recording the login page as the baseline would make the next real reading
    look like a change, and the one after it look like nothing happened.
    """
    workload = _workload(tmp_path)
    workload.evaluate(_run(reply="LOGIN\n\nwants a password"))

    assert not (tmp_path / "last-seen.json").exists()


def test_signing_in_sends_the_bot_round_again(tmp_path: Any) -> None:
    """The profile is persistent, so the session a person just made is live."""
    state, reason = _workload(tmp_path).apply(
        _run(), "approve", {"choice": "I signed in — look again"}
    )

    assert state == "continue"
    assert "signed in" in reason


def test_the_brief_forbids_the_bot_handling_credentials(tmp_path: Any) -> None:
    """The instruction has to be explicit, and it has to close the side doors.

    "Do not type a password" alone leaves looking one up in the workspace on
    the table, which is the interesting failure.
    """
    brief = _workload(tmp_path)._brief(_run())

    assert "Do not type a password" in brief
    assert "do not" in brief and "try to find one" in brief
    assert "LOGIN" in brief


def test_the_brief_explains_refs(tmp_path: Any) -> None:
    """Snapshots name what is clickable; a brief that omits it wastes the feature."""
    brief = _workload(tmp_path)._brief(_run())

    assert "[ref=N]" in brief
    assert "snapshot_id" in brief


def test_both_browsing_workloads_carry_the_same_rules(tmp_path: Any) -> None:
    """One text, two briefs. Written twice they drift, and the drift is silent.

    That is not hypothetical here: the watcher labelled its own sessions with
    the bot's browser profile and the research workload did not, so nine of the
    ten bots in the crypto example had no browser and nothing said so. A page
    can lie and a page can ask for a credential whoever is reading it.
    """
    from army.bots.workloads.browsing import ASK_FOR_A_SIGN_IN, DATA_NOT_COMMAND
    from army.bots.workloads.research import ResearchWorkload

    watch = _workload(tmp_path)._brief(_run())
    research = ResearchWorkload(repo=str(tmp_path))._brief("why", _run())

    for brief in (watch, research):
        assert DATA_NOT_COMMAND in brief
        assert ASK_FOR_A_SIGN_IN in brief
        assert "[ref=N]" in brief


def test_a_replaced_baseline_is_still_readable(tmp_path: Any) -> None:
    """A wrong baseline is how a watcher goes quietly blind.

    "CHANGED — the page now shows a login wall" leads with CHANGED, so it
    becomes the baseline; nothing inside the workload can tell that apart from
    a real reading, and prose-sniffing the rest would be worse than the bug.
    What can be done is keep the thing it replaced, in the bot's own workspace
    where the Files panel shows it — so "why has this been quiet for a week" is
    answerable rather than a mystery.
    """
    workload = _workload(tmp_path)
    workload.evaluate(_run(reply="CHANGED\n\nTier 1: 0.40% / 0.80%"))
    workload.evaluate(_run(reply="CHANGED — the page now shows a login wall"))

    kept = json.loads((tmp_path / "last-seen.json").read_text())
    assert "login wall" in kept["summary"]
    assert "0.40%" in kept["previously"], "the good baseline survives its replacement"


def test_an_unparseable_verdict_asks_rather_than_going_quiet(tmp_path: Any) -> None:
    """No-news and cannot-tell are the same silence, and must not be.

    An agent that opens "Here's what I found:" produced no verdict, which read
    as UNCHANGED — so a watcher that had stopped working looked exactly like
    one with nothing to report.
    """
    for reply in ("Here's what I found:", "", "Please log in to continue"):
        question, options, evidence = _workload(tmp_path).evaluate(_run(reply=reply))
        assert question, f"{reply!r} must not read as no-news"
        assert options == ["look again", "stop"]
        assert "unparsed" in evidence


def test_the_ordinary_verdicts_still_work(tmp_path: Any) -> None:
    """The parser got stricter; it must not have got narrower."""
    for reply, expect_question in (
        ("CHANGED\n\n0.40%", True),
        ("**CHANGED**\n\n0.40%", True),
        ("### Unchanged — same as yesterday", False),
        ("UNCHANGED", False),
        ("LOGIN\n\nwants a password", True),
    ):
        question, _, _ = _workload(tmp_path).evaluate(_run(reply=reply))
        assert bool(question) is expect_question, reply


def test_the_workspace_is_made_before_the_session_opens(tmp_path: Any) -> None:
    """The server refuses a session on a directory that is not there.

    And the refusal names the *host* — "workspace path does not exist on host
    'HUL-0095.local'" — which lands one layer below anything the operator
    wrote, in a run that reads as a broken harness. A fresh clone of an example
    should not need a `mkdir` nobody documented.
    """
    workspace = tmp_path / "not-yet"
    workload = WatchWorkload(url="https://example.com", question="q", workspace=str(workspace))

    workload.dispatch(_run(), FakeOmni())

    assert workspace.is_dir()


def test_a_crash_before_collecting_does_not_open_a_second_session(tmp_path: Any) -> None:
    """The recovery story is only true for the states after DISPATCHING.

    `dispatch` creates a session; if the process dies before the run reaches
    COLLECTING, the next tick calls `dispatch` again — orphaning the first
    session and paying for a second agent turn on the same work. `find_session`
    was written for exactly this and only a demo workload ever called it, so
    both real workloads had the bug.
    """
    omni = FakeOmni()
    run = _run(outstanding=[])
    workload = _workload(tmp_path)

    first = workload.dispatch(run, omni)
    second = workload.dispatch(run, omni)

    assert first == second, "the retry must pick the session back up"
    assert len(omni.sessions) == 1


def test_two_different_runs_get_two_sessions(tmp_path: Any) -> None:
    """A watcher's title is the page it watches, identical every iteration.

    Deduplicating on that alone would hand a bot last week's session and never
    open a new one — so the key carries the run.
    """
    omni = FakeOmni()
    workload = _workload(tmp_path)

    monday = workload.dispatch(_run(), omni)
    tuesday = workload.dispatch(
        Run(
            id="b" * 32,
            workflow="w",
            state=RunState.DISPATCHING,
            version=1,
            attempt=1,
            created_at=NOW,
            updated_at=NOW,
            payload={},
            artifacts={},
            outstanding=[],
            bot_id="bot",
        ),
        omni,
    )

    assert monday != tuesday
