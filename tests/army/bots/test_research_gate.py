"""What the research gate is actually deciding."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from army.bots.workloads.research import ResearchWorkload
from army.state import Run, RunState

NOW = 1_700_000_000


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "reports").mkdir(parents=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "x@y"],
        ["config", "user.name", "x"],
        ["checkout", "-q", "-b", "bots/scout"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "seed", "--no-verify"], cwd=repo, check=True, capture_output=True
    )
    return repo


def _run(**payload: Any) -> Run:
    return Run(
        id="c" * 32,
        workflow="army.bots.workloads.research:ResearchWorkload",
        state=RunState.EVALUATING,
        version=1,
        attempt=1,
        created_at=NOW,
        updated_at=NOW,
        payload={"question": "does it hold?", **payload},
        artifacts={"reply": "it holds"},
        outstanding=[],
        bot_id="bot",
    )


def test_the_question_says_the_work_is_already_recorded(tmp_path: Path) -> None:
    """A gate that reads as "may I commit" but answers "keep going" is a trap.

    The commit lands on the bot's own worktree branch and is never pushed, so
    a `stop` ends the next iteration rather than undoing this one. Somebody
    reading the card has to be able to tell.
    """
    repo = _repo(tmp_path)
    (repo / "finding.md").write_text("a finding\n")
    workload = ResearchWorkload(repo=str(repo))

    question, options, evidence = workload.evaluate(_run())

    assert "Keep going?" in question
    assert options == ["continue", "stop"]
    assert "does not undo this one" in evidence["already recorded"]


def test_a_commit_that_did_not_land_is_not_reported_as_one(tmp_path: Path) -> None:
    """`rev-parse HEAD` answers whether or not the commit worked.

    So returning it blind reported the *previous* sha as this iteration's
    work — an approval card naming a commit that does not contain what it
    describes, which is worse than no sha at all.
    """
    repo = _repo(tmp_path)
    (repo / "finding.md").write_text("a finding\n")
    workload = ResearchWorkload(repo=str(repo))
    real = workload._git

    def _commit_never_lands(*args: str) -> str | None:
        # Everything works except the commit itself — a failing hook, a missing
        # identity, a full disk. HEAD stays where it was.
        return None if args and args[0] == "commit" else real(*args)

    workload._git = _commit_never_lands  # type: ignore[method-assign]

    assert workload._commit("does it hold?") is None


def test_a_commit_that_landed_is_reported(tmp_path: Path) -> None:
    """The fix must not make every commit look like a failure."""
    repo = _repo(tmp_path)
    (repo / "finding.md").write_text("a finding\n")

    landed = ResearchWorkload(repo=str(repo))._commit("does it hold?")

    assert landed and len(landed) >= 7
