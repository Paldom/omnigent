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


def test_the_browser_profile_label_rides_on_the_session(tmp_path: Any) -> None:
    """The label is the whole routing decision; without it there is no gateway."""
    seen: dict[str, Any] = {}

    class Recording(FakeOmni):
        def create_session(self, agent_id: str, **kwargs: Any) -> str:
            seen.update(kwargs)
            return super().create_session(agent_id, **kwargs)

    workload = _workload(tmp_path, profile="bot-kraken-fee-watch", agent="claude-native-ui")
    workload.dispatch(_run(), Recording())

    assert seen["labels"] == {"omnigent.browser.profile": "bot-kraken-fee-watch"}
