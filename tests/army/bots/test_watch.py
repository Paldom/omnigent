"""The watcher's first live run, and the three ways it lied about itself.

One run of ``kraken-fee-watch`` produced a correct, well-sourced answer and
recorded ``completed · nothing to report``. Three separate defects had to line
up for that, and none of them raised: a wrong accessor, a wrong reader, and a
parser stricter than the thing it parses. Each is pinned here by its failure.
"""

from __future__ import annotations

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
