"""A gate that offers several answers can be given several answers.

The barrier is the non-blocking half of human-in-the-loop: the question goes
into the session, the turn ends, and the supervisor picks the answer up on a
later tick. That made it survive an overnight wait, and it also made it poorer
than the blocking elicitation card, which has had checkboxes and a free-text
row all along — the barrier took exactly one option and refused anything else.

What is deliberately *not* relaxed is the ambiguity rule. A workload has to say
``multi_select = True`` before two named options count as two answers; left
unsaid, "merge or iterate, I can't decide" stays unanswered, which is the only
safe reading on a gate that arms money.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from army.state import Run, RunState
from army.store import Store
from army.supervisor import Supervisor
from tests.army.test_supervisor import ChattyOmni, DemoWorkload, FakeOmni, _drive_to_waiting


class SnackWorkload(DemoWorkload):
    """A gate whose options are things you may want several of at once."""

    name = "snacks"
    multi_select = True

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:
        return "Pick snacks", ["popcorn", "pretzels", "olives"], dict(run.artifacts)

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:
        if decision == "deny":
            return "paused", "owner declined"
        picked = payload.get("choices") or []
        return "continue", f"owner chose {', '.join(picked)}"


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    """A store on a real SQLite file, matching the sibling suite."""
    return Store(tmp_path / "army.db")


def _args(choice: list[str] | None = None, text: str | None = None) -> argparse.Namespace:
    """An argparse namespace shaped like the answer subcommand's."""
    return argparse.Namespace(choice=choice, text=text)


def _payload(run: Run, args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Call the CLI's payload builder.

    Imported here rather than at module scope so a tree without the feature
    still collects this file — the chat-path tests above then fail on
    behaviour, which is the more informative failure.
    """
    from army.cli import _answer_payload

    return _answer_payload(run, args)


# ── the chat path ────────────────────────────────────────────


def test_two_options_named_on_a_multi_select_gate_are_two_answers(store: Store) -> None:
    """The feature: "popcorn and pretzels" means both, not "unclear"."""
    omni, workload = ChattyOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)
    omni.replies = ["popcorn and pretzels please"]

    Supervisor(store, omni, workload).tick()

    after = store.get_run(run.id)
    assert after is not None
    assert after.state is RunState.CONTINUE
    assert after.terminal_reason == "owner chose popcorn, pretzels"


def test_a_pick_one_gate_still_refuses_two(store: Store) -> None:
    """The rule the multi-select opt-in exists to preserve.

    ``DemoWorkload`` says nothing about multi-select, so naming two options
    leaves the run parked exactly as before — the answer is ambiguous and a
    gate that guesses is worse than one that waits.
    """
    omni, workload = ChattyOmni(), DemoWorkload()
    run = _drive_to_waiting(store, omni, workload)
    omni.replies = ["ship or iterate, I can't decide"]

    Supervisor(store, omni, workload).tick()

    after = store.get_run(run.id)
    assert after is not None
    assert after.state is RunState.WAITING_HUMAN


def test_one_option_on_a_multi_select_gate_still_sets_choice(store: Store) -> None:
    """A single pick keeps the old payload key, so old workloads keep working."""
    omni, workload = ChattyOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)
    omni.replies = ["olives"]

    Supervisor(store, omni, workload).tick()

    after = store.get_run(run.id)
    assert after is not None
    assert after.terminal_reason == "owner chose olives"


@pytest.mark.parametrize(
    "reply",
    [
        # Refusal scoped over a list: the "don't" attaches to neither option,
        # so adjacency reads both as wanted — the exact inversion.
        "I don't want popcorn or pretzels",
        "no popcorn or pretzels for me",
        "never popcorn or olives",
        # Affirmation and refusal mixed. Readable to a person, and the scope
        # is still guesswork to a matcher.
        "popcorn but not olives",
        # A refusal word with no negation attached to either option.
        "reject popcorn and pretzels",
        # A question. The pick-one gate caught these by refusing any reply
        # that named two options; multi-select has to catch them explicitly.
        "popcorn or pretzels - which do you recommend?",
        "can I have popcorn and pretzels?",
    ],
)
def test_a_reply_that_names_options_without_choosing_them_stays_parked(
    store: Store, reply: str
) -> None:
    """Naming options is not choosing them.

    The matcher only sees the word before an option, so "don't want popcorn or
    pretzels" marks neither as negated and would otherwise pick both — turning
    a refusal into approval of everything refused. A pick-one gate caught all
    of these by refusing any reply that named two options; multi-select gives
    that guard up, so the refusal and the question mark are checked instead.
    """
    omni, workload = ChattyOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)
    omni.replies = [reply]

    Supervisor(store, omni, workload).tick()

    after = store.get_run(run.id)
    assert after is not None
    assert after.state is RunState.WAITING_HUMAN, f"{reply!r} was taken as an answer"


def test_a_deny_records_even_with_a_mistyped_choice(store: Store) -> None:
    """Stopping something must not be blocked by validation of an unused flag.

    ``army deny --choice olvies`` is someone trying to halt a run. Refusing to
    record it over the typo would leave the thing they are stopping running,
    which is the wrong direction for this to fail.
    """
    omni, workload = FakeOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)

    payload, refusal = _payload(run, _args(choice=["olvies"]))

    # The refusal is reported, but ``cmd_answer`` records the decline anyway.
    assert refusal is not None
    assert payload == {}


def test_a_repeated_choice_is_counted_once(store: Store) -> None:
    """``--choice popcorn --choice popcorn`` is one pick, not two.

    Otherwise it would trip the pick-one guard on a gate that offers one.
    """
    omni, workload = FakeOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)

    payload, refusal = _payload(run, _args(choice=["popcorn", "popcorn"]))

    assert refusal is None
    assert payload == {"choices": ["popcorn"], "choice": "popcorn"}


def test_refusing_every_named_option_is_a_decline(store: Store) -> None:
    """Naming options only to reject them is a no, not an empty approval."""
    omni, workload = ChattyOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)
    omni.replies = ["not popcorn, not olives"]

    Supervisor(store, omni, workload).tick()

    after = store.get_run(run.id)
    assert after is not None
    assert after.state is RunState.PAUSED
    assert after.terminal_reason == "owner declined"


def test_the_gate_shape_is_recorded_when_parking(store: Store) -> None:
    """The CLI reads it from the run, so it never has to load the workload."""
    omni, workload = FakeOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)

    assert run.artifacts["multi_select"] is True
    assert run.artifacts["options"] == ["popcorn", "pretzels", "olives"]


def test_a_single_select_gate_records_that_too(store: Store) -> None:
    """Absent means false, and it is written down rather than inferred."""
    omni, workload = FakeOmni(), DemoWorkload()
    run = _drive_to_waiting(store, omni, workload)

    assert run.artifacts["multi_select"] is False


# ── army approve ─────────────────────────────────────────────


def test_repeated_choice_builds_a_multi_answer(store: Store) -> None:
    """``--choice popcorn --choice pretzels`` is the terminal half."""
    omni, workload = FakeOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)

    payload, refusal = _payload(run, _args(choice=["popcorn", "pretzels"]))

    assert refusal is None
    assert payload == {"choices": ["popcorn", "pretzels"]}


def test_one_choice_carries_both_keys(store: Store) -> None:
    """``choices`` for the new readers, ``choice`` for every existing one."""
    omni, workload = FakeOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)

    payload, refusal = _payload(run, _args(choice=["olives"]))

    assert refusal is None
    assert payload == {"choices": ["olives"], "choice": "olives"}


def test_free_text_travels_alongside(store: Store) -> None:
    """Some gates want a sentence, not a pick — the payload carries both."""
    omni, workload = FakeOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)

    payload, refusal = _payload(run, _args(choice=["popcorn"], text="and something salty"))

    assert refusal is None
    assert payload["text"] == "and something salty"
    assert payload["choices"] == ["popcorn"]


def test_text_alone_is_an_answer(store: Store) -> None:
    """A free-form reply with no pick is still a reply."""
    omni, workload = FakeOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)

    payload, refusal = _payload(run, _args(text="surprise me"))

    assert refusal is None
    assert payload == {"text": "surprise me"}


def test_a_second_choice_on_a_pick_one_gate_is_refused(store: Store) -> None:
    """Said at the terminal, the same rule the chat path keeps."""
    omni, workload = FakeOmni(), DemoWorkload()
    run = _drive_to_waiting(store, omni, workload)

    payload, refusal = _payload(run, _args(choice=["ship", "iterate"]))

    assert payload == {}
    assert refusal is not None
    assert "takes one choice" in refusal


def test_an_option_the_gate_never_offered_is_refused(store: Store) -> None:
    """A typo now fails while you are still there to retype it.

    Recorded instead, it would park the run again one tick later with the
    workload rejecting it — the same outcome, an hour of loop time apart.
    """
    omni, workload = FakeOmni(), SnackWorkload()
    run = _drive_to_waiting(store, omni, workload)

    payload, refusal = _payload(run, _args(choice=["popcorns"]))

    assert payload == {}
    assert refusal is not None
    assert "not an option: popcorns" in refusal
    assert "popcorn, pretzels, olives" in refusal


def test_the_question_asks_for_what_the_gate_accepts(store: Store) -> None:
    """The instruction the owner reads has to match the gate.

    A multi-select gate that tells them to "reply with one option" is telling
    them the barrier cannot do the thing it just gained.
    """
    omni, workload = FakeOmni(), SnackWorkload()
    _drive_to_waiting(store, omni, workload)

    assert omni.asked_multi == [True]


def test_a_pick_one_gate_still_asks_for_one(store: Store) -> None:
    """And the unchanged case stays unchanged."""
    omni, workload = FakeOmni(), DemoWorkload()
    _drive_to_waiting(store, omni, workload)

    assert omni.asked_multi == [False]


class YesNoWorkload(DemoWorkload):
    """The commonest gate shape there is."""

    name = "yesno"

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:
        return "Proceed?", ["yes", "no"], dict(run.artifacts)

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:
        if decision == "deny":
            return "paused", "owner declined"
        return "continue", f"owner said {payload.get('choice')}"


def test_a_yes_no_gate_can_be_answered_no(store: Store) -> None:
    """ "no" is an option here, not a refusal word.

    The refusal guard carves out whatever the gate offered. Without that, the
    most common gate in human-in-the-loop cannot be answered from the chat
    path at all — the answer parks and the run waits for a person who thinks
    they already replied.
    """
    omni, workload = ChattyOmni(), YesNoWorkload()
    run = _drive_to_waiting(store, omni, workload)
    omni.replies = ["no"]

    Supervisor(store, omni, workload).tick()

    after = store.get_run(run.id)
    assert after is not None
    assert after.state is RunState.CONTINUE
    assert after.terminal_reason == "owner said no"


@pytest.mark.parametrize("reply", ["ship it, no rush", "ship, why not?", "ship - no doubt"])
def test_a_pick_one_gate_is_unchanged_by_the_refusal_guard(store: Store, reply: str) -> None:
    """The guard belongs to multi-select gates only.

    A pick-one gate already refuses any reply naming two options, which is the
    bail the guard stands in for. Running it there too would park ordinary
    answers that carry a stray "no" or a question mark — a silent behaviour
    change on the path this feature was not supposed to touch.
    """
    omni, workload = ChattyOmni(), DemoWorkload()
    run = _drive_to_waiting(store, omni, workload)
    omni.replies = [reply]

    Supervisor(store, omni, workload).tick()

    after = store.get_run(run.id)
    assert after is not None
    assert after.state is not RunState.WAITING_HUMAN, f"{reply!r} should still answer"


def test_a_framework_notice_is_not_an_answer() -> None:
    """A fired timer and a sub-agent wake arrive as user-role text.

    Both carry words the agent chose — a timer note, a child's title — and
    both are `is_meta`, so they are hidden from the transcript while still
    being user-role. Reading one as the answer would let an agent approve its
    own gate through ordinary tool use.
    """
    from army.omni import OmniClient, barrier_marker

    marker = barrier_marker("run1")
    snapshot = {
        "items": [
            {"type": "message", "data": {"role": "user", "content": marker}},
            {"type": "message", "data": {"role": "user", "content": "popcorn", "is_meta": True}},
        ],
        "pending_inputs": [],
    }

    omni = OmniClient(base_url="http://localhost:1")
    omni._request = lambda *a, **k: snapshot  # type: ignore[method-assign]

    assert omni.replies_after("s1", marker) == []
