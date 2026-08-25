"""Shared fakes for the bot fleet.

Fakes rather than mocks, matching ``tests/army/``: a mock asserts that a call
happened, which tells you the code you wrote is the code you wrote. A fake lets
the test assert what the *system* ended up believing, which is the only thing
that survives a rewrite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from army.bots.model import Bot, BotStatus, WakeKind, WakePolicy
from army.bots.registry import WorkloadRegistry
from army.bots.schedule import first_wake
from army.bots.store import BotStore
from army.omni import Session
from army.state import Run
from army.store import Store


class FakeOmni:
    """Stands in for the Omnigent server, recording what it was asked to do."""

    def __init__(self) -> None:
        self.sessions: list[str] = []
        self.asked: list[tuple[str, str, list[str]]] = []
        self.harness: str | None = None
        self.tail: str = ""
        #: What a person typed into the session after the question. The bot
        #: supervisor must read these and refuse to treat them as an answer.
        self.replies: list[str] = []
        #: What the *agent* said. Kept separate from :attr:`replies` on
        #: purpose: a fake that returns one list for both roles lets a
        #: workload read human answers as its agent's work and still pass.
        self.agent_replies: list[str] = []

        #: Labels the supervisor asked every session to carry, via
        #: :meth:`with_labels`. Recorded rather than merged so a test can see
        #: what the fleet decided, not only what a workload passed.
        self.default_labels: dict[str, str] = {}
        #: Every ``create_session`` call's labels, in order.
        self.labelled: list[dict[str, str]] = []
        #: Every ``create_session`` call's host, in order.
        self.hosts: list[str | None] = []
        #: Session id by title, so ``find_session`` answers like the server.
        self.titles: dict[str, str] = {}
        #: What the framework prefaced this run's body with — the assembled
        #: memory. Recorded because "the briefing reaches the body" is the
        #: whole claim, and it was false for as long as nothing checked.
        self.preamble: str = ""

    def with_briefing(self, preamble: str) -> FakeOmni:
        self.preamble = preamble
        return self

    def with_labels(self, labels: dict[str, str]) -> FakeOmni:
        self.default_labels = {**self.default_labels, **labels}
        return self

    def create_session(self, agent_id: str, **kwargs: Any) -> str:
        session_id = f"conv_{len(self.sessions):032d}"
        self.sessions.append(session_id)
        # Titles are how the server makes session creation idempotent, so a
        # fake that forgets them cannot show a re-dispatch reusing one.
        if kwargs.get("title"):
            self.titles[str(kwargs["title"])] = session_id
        self.labelled.append({**self.default_labels, **(kwargs.get("labels") or {})})
        #: What each session was pinned to. A session with no host gets no
        #: runner and fails in a way that reads like a broken harness.
        self.hosts.append(kwargs.get("host_id"))
        return session_id

    def send(self, session_id: str, text: str) -> None:
        return None

    def get_session(self, session_id: str) -> Session:
        return Session(
            id=session_id,
            title=None,
            status="idle",
            harness=self.harness,
            pending_elicitations=[],
            tail=self.tail,
        )

    def replies_after(self, session_id: str, marker: str) -> list[str]:
        return list(self.replies)

    def agent_said(self, session_id: str) -> list[str]:
        return list(self.agent_replies)

    def was_told(self, session_id: str, text: str) -> bool:
        return True

    def open_once(self, run_id: str, agent_id: str, *, title: str, **kwargs: Any) -> str:
        marked = f"{title} · {run_id[:8]}"
        return self.find_session(marked) or self.create_session(agent_id, title=marked, **kwargs)

    def find_session(self, title: str) -> str | None:
        return self.titles.get(title)

    def resolve_agent(self, name_or_id: str) -> str:
        return f"ag_{name_or_id}"

    def ask(
        self,
        run_id: str,
        session_id: str,
        message: str,
        options: list[str],
        *,
        evidence: dict[str, Any] | None = None,
    ) -> str:
        self.asked.append((session_id, message, options))
        return f"elicit_{len(self.asked)}"


class CountingWorkload:
    """A workload with a finite queue that finishes as soon as it is collected.

    :param name: Workflow name, so two bots can be given distinct ones.
    :param items: The queue. Each ``acquire`` takes one.
    """

    def __init__(self, name: str = "counting", items: list[dict[str, Any]] | None = None) -> None:
        self.name = name
        self.items = list(items if items is not None else [{"task": "one"}])
        self.acquired: list[dict[str, Any]] = []
        self.harness: str | None = None
        self.next_outcome: str | None = None

    def acquire(self) -> dict[str, Any] | None:
        if not self.items:
            return None
        item = self.items.pop(0)
        self.acquired.append(item)
        return item

    def dispatch(self, run: Run, omni: Any) -> list[str]:
        return [omni.create_session("ag_worker")]

    def collect(self, run: Run, omni: Any) -> tuple[bool, dict[str, Any]]:
        artifacts: dict[str, Any] = {"approval_session_id": run.outstanding[0]}
        if self.next_outcome is not None:
            artifacts["outcome"] = self.next_outcome
        return True, artifacts

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:
        return "Continue?", ["yes", "stop"], {}

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:
        if decision == "deny":
            return "paused", "declined"
        if payload.get("choice") == "stop":
            return "completed", "stopped"
        return "continue", "approved"


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    """A run store on a real SQLite file, so restarts can be simulated honestly."""
    return Store(tmp_path / "army.db")


@pytest.fixture()
def bots(store: Store) -> BotStore:
    """A bot store sharing that file, which is what makes succession atomic."""
    return BotStore(store)


@pytest.fixture()
def registry() -> WorkloadRegistry:
    """A registry that permits the test module's own workloads."""
    return WorkloadRegistry(allow=set())


def make_bot(
    slug: str = "scout",
    *,
    workload: str = "tests:counting",
    wake: WakePolicy | None = None,
    now: int = 1_700_000_000,
    **extra: Any,
) -> Bot:
    """
    Define a bot with workable defaults.

    :param slug: Its addressable name.
    :param workload: Dotted path; tests usually override resolution instead.
    :param wake: Wake policy; defaults to a daily rrule.
    :param now: Epoch seconds.
    :param extra: Any other :class:`~army.bots.model.Bot` field.
    :returns: The bot, not yet persisted.
    """
    return Bot.new(
        slug,
        persona="a standing role",
        mission="do the thing",
        workload=workload,
        wake=wake or WakePolicy(kind=WakeKind.RRULE, rrule="FREQ=DAILY;BYHOUR=9"),
        created_by="human:test",
        now=now,
        **extra,
    )


def activate(bots: BotStore, bot: Bot, *, now: int = 1_700_000_000) -> Bot:
    """
    Persist a bot and move it to ``ACTIVE`` with its first wake.

    :param bots: The bot store.
    :param bot: The bot.
    :param now: Epoch seconds.
    :returns: The activated bot.
    """
    bots.create(bot)
    return bots.set_status(bot, BotStatus.ACTIVE, now=now, wake=first_wake(bot.wake, now=now))


class StubRegistry(WorkloadRegistry):
    """A registry returning workloads handed to it, bypassing imports.

    :param mapping: ``{dotted_path: workload}``.
    """

    def __init__(self, mapping: dict[str, Any]) -> None:
        super().__init__(allow=set(mapping))
        self._mapping = mapping

    def resolve(self, path: str, options: dict[str, Any] | None = None) -> Any:
        return self._mapping[path]
