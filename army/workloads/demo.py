"""A working loop you can point at a real repository today.

Reads tasks from a plain text file, gives each one to an implementer on one
vendor, has a *different* vendor review the result, and puts the pair in front
of you before anything continues. That is the shape every workload has; the
domain words are the only part that changes.

Cross-vendor review is the point rather than a flourish: a diff judged by the
model that wrote it is judged by its own blind spots. Routing the review to a
different vendor is close to free once you already have both logged in.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from army.omni import OmniClient
from army.state import Run

_logger = logging.getLogger(__name__)


class DemoWorkload:
    """Take one task, implement it, review it on another vendor, then ask.

    :param queue_path: Text file of tasks, one per line. Blank lines and lines
        starting with ``#`` are skipped, and a completed task is marked with a
        leading ``done:`` rather than deleted, so the file stays an audit trail.
    :param implementer_agent: Agent name or id for the vendor that writes the
        change. Names are the ones in ``GET /v1/agents``.
    :param reviewer_agent: The vendor that judges it. Point this at a different
        vendor than the implementer — a diff judged by the model that wrote it
        is judged by its own blind spots.
    :param workspace: Directory the sessions run in.
    :param host_id: Host to pin sessions to. ``None`` asks the server for its
        first online host, which is right for a single box.
    :param harness: Harness override for both roles. Leave unset to use each
        agent's own. Worth setting to a headless harness on a box with no
        working terminal — a native TUI harness needs tmux, and fails the turn
        without it.
    :param worktrees: Directory to create per-run git worktrees under. Set it
        and the implementer works in its own checkout on its own branch, so two
        iterations cannot edit the same files. Unset, everything shares
        ``workspace`` — which is fine for one run at a time and wrong the
        moment there are two.
    """

    name = "demo"

    #: The choices offered at the barrier. ``apply`` refuses anything else
    #: rather than guessing, so this is the whole accepted set.
    OPTIONS: tuple[str, ...] = ("merge", "iterate", "discard", "stop")

    def __init__(
        self,
        queue_path: str = "army-queue.txt",
        implementer_agent: str = "claude-native-ui",
        reviewer_agent: str = "codex-native-ui",
        workspace: str | None = None,
        host_id: str | None = None,
        harness: str | None = None,
        worktrees: str | None = None,
    ) -> None:
        self.queue_path = Path(queue_path).expanduser()
        self.implementer_agent = implementer_agent
        self.reviewer_agent = reviewer_agent
        self.workspace = workspace
        self.host_id = host_id
        self.harness = harness
        self.worktrees = Path(worktrees).expanduser() if worktrees else None
        # Resolved once per process, on first use. The names in army.toml are
        # what a person writes; the API wants the ids it minted.
        self._agent_ids: dict[str, str] = {}

    def _worktree_for(self, run: Run) -> str | None:
        """
        Give this run its own checkout, if worktrees are configured.

        Two agents editing one checkout produce a mess neither can explain, and
        it is the kind of mess that only shows up when two iterations overlap —
        which is exactly when nobody is watching. The branch is named for the
        run, so an abandoned one is identifiable later.

        Existing worktrees are reused, so a re-dispatch after a crash goes back
        to the same checkout rather than starting a third one.

        They are deliberately not cleaned up: the branch and its uncommitted
        state are the iteration's output, and deleting that on a failure path
        would throw away the evidence of what went wrong. ``git worktree
        prune`` when you have read them.

        :param run: The run being dispatched.
        :returns: A path to use as the session workspace, or ``None`` to use
            the shared one.
        """
        if self.worktrees is None or not self.workspace:
            return None
        target = self.worktrees / run.id[:12]
        if target.exists():
            return str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "worktree", "add", "-b", f"army/{run.id[:12]}", str(target)],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Not fatal: a repo that cannot make a worktree can still be worked
            # in directly. Say so, because the isolation the roster promises is
            # now absent.
            _logger.warning(
                "could not create a worktree for run %s (%s); falling back to %s",
                run.id[:12],
                result.stderr.strip()[:160],
                self.workspace,
            )
            return None
        return str(target)

    def _host(self, omni: OmniClient) -> str | None:
        """Resolve the host to pin sessions to, asking the server if unset."""
        if self.host_id is None:
            self.host_id = omni.default_host()
        return self.host_id

    def _agent_id(self, name: str, omni: OmniClient) -> str:
        """Resolve a configured agent name to its id, caching the answer."""
        if name not in self._agent_ids:
            self._agent_ids[name] = omni.resolve_agent(name)
        return self._agent_ids[name]

    # ── the five seams ────────────────────────────────────────────

    def acquire(self) -> dict[str, Any] | None:
        """Take the first unfinished line from the queue file.

        Marks the line ``taken:``, not ``done:``. Writing ``done:`` here would
        claim the work finished before it had started — and a run that then
        fails (three dispatch attempts, a vanished session, the stall guard)
        leaves the file asserting success for a task nobody did. ``taken:``
        says what is true: this was picked up. :meth:`apply` converts it to
        ``done:`` when the owner accepts, or back to a bare task line when they
        ask for another pass.

        A line left at ``taken:`` is therefore a task whose iteration did not
        finish. That is visible in the file, next to a FAILED run in
        ``army status``, and re-queued by deleting the prefix.
        """
        if not self.queue_path.exists():
            return None
        lines = self.queue_path.read_text().splitlines()
        for index, line in enumerate(lines):
            task = line.strip()
            if not task or task.startswith(("#", "done:", "taken:")):
                continue
            lines[index] = f"taken: {task}"
            self.queue_path.write_text("\n".join(lines) + "\n")
            return {"task": task, "line": index}
        return None

    def dispatch(self, run: Run, omni: OmniClient) -> list[str]:
        """Start the implementer on the task.

        Only the implementer starts here. The reviewer is spawned in
        :meth:`collect` once there is something to review — starting it now
        would hold a second vendor lane open doing nothing.
        """
        task = run.payload.get("task", "")
        # The title carries the run id so it is a stable idempotency key: a
        # dispatch that crashed after creating the session finds it again on
        # the retry instead of orphaning it and starting a second one.
        title = f"{_title(task)}-{run.id[:8]}"
        session_id = omni.find_session(title)
        if session_id is None:
            session_id = omni.create_session(
                self._agent_id(self.implementer_agent, omni),
                title=title,
                workspace=self._worktree_for(run) or self.workspace,
                host_id=self._host(omni),
                harness=self.harness,
            )
        # Creating the session and sending the task are two calls, so a crash
        # between them leaves a session that was never told what to do. Reusing
        # it without checking strands the run until the stall timer, which looks
        # exactly like an agent thinking hard. Ask whether the task arrived.
        if not omni.was_told(session_id, task):
            omni.send(
                session_id,
                f"{task}\n\n"
                "Work on a branch. Commit when the change is coherent and the "
                "project's own checks pass. Reply with a summary of what you "
                "changed and why, and the branch name.",
            )
        return [session_id]

    def collect(self, run: Run, omni: OmniClient) -> tuple[bool, dict[str, Any]]:
        """Wait for the implementer, then run the review on another vendor."""
        artifacts: dict[str, Any] = {}
        implementer_id = run.outstanding[0] if run.outstanding else None
        if implementer_id is None:
            return True, {"error": "nothing was dispatched"}

        implementer = omni.get_session(implementer_id)
        if implementer.status == "running":
            return False, {}

        artifacts["implementer_session"] = implementer_id
        # The approval belongs on the implementer's session: that is where the
        # work and its transcript are, so the question lands in context.
        artifacts["approval_session_id"] = implementer_id

        reviewer_id = run.artifacts.get("reviewer_session")
        if reviewer_id is None:
            review_title = f"review-{_title(run.payload.get('task', ''))}-{run.id[:8]}"
            reviewer_id = omni.find_session(review_title) or omni.create_session(
                self._agent_id(self.reviewer_agent, omni),
                title=review_title,
                workspace=self.workspace,
                host_id=self._host(omni),
                harness=self.harness,
            )
            omni.send(
                reviewer_id,
                "Review the change just made on this repository by another "
                "agent. Read the diff on its branch. Say plainly whether it "
                "does what was asked, what you would change, and whether you "
                "would merge it. Do not make the change yourself.",
            )
            artifacts["reviewer_session"] = reviewer_id
            return False, artifacts

        reviewer = omni.get_session(str(reviewer_id))
        if reviewer.status == "running":
            return False, artifacts
        artifacts["reviewer_session"] = reviewer_id
        return True, artifacts

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:
        """Put the implementation and its cross-vendor review side by side."""
        task = run.payload.get("task", "the task")
        evidence = {
            "task": task,
            "implementer_session": run.artifacts.get("implementer_session"),
            "reviewer_session": run.artifacts.get("reviewer_session"),
            "attempt": run.attempt,
        }
        return (
            f"{task}\n\nImplemented and reviewed by a different vendor. Continue?",
            list(self.OPTIONS),
            evidence,
        )

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:
        """Turn the answer into the loop's next move.

        A decline pauses this branch and nothing else. It never falls through
        to an approval — a gate that approves when unanswered is not a gate.
        """
        if decision == "deny":
            # The line stays `taken:`. A paused run is resumable — `army resume`
            # sends this same run back to READY — so the run still holds the
            # claim, and returning the task to the queue would let the next tick
            # open a second run for work somebody just declined.
            return "paused", "declined; the branch is paused until `army resume`"

        choice = str(payload.get("choice") or "")
        if choice not in self.OPTIONS:
            # An unrecognised choice must not fall through to "approved". The
            # barrier exists to be answered deliberately; a typo is not an
            # answer, and defaulting one to merge is the worst possible guess.
            return "paused", f"unrecognised choice {choice!r}; paused for a real answer"
        if choice == "stop":
            self._mark_done(run)
            return "completed", "owner stopped the loop"
        if choice == "discard":
            self._mark_done(run)
            return "continue", "change discarded; moving on"
        if choice == "iterate":
            self._requeue(run)
            return "continue", "task returned to the queue for another pass"
        self._mark_done(run)
        return "continue", "approved"

    def _mark_done(self, run: Run) -> None:
        """Convert this run's ``taken:`` line to ``done:`` once it is settled."""
        self._rewrite_line(run, lambda task: f"done: {task}")

    # ── queue bookkeeping ─────────────────────────────────────────

    def _requeue(self, run: Run) -> None:
        """Put a task back so the next iteration picks it up again."""
        self._rewrite_line(run, lambda task: task)

    def _rewrite_line(self, run: Run, render: Callable[[str], str]) -> None:
        """Rewrite this run's queue line, if it is still where it was."""
        task = run.payload.get("task")
        index = run.payload.get("line")
        if not task or not isinstance(index, int) or not self.queue_path.exists():
            return
        lines = self.queue_path.read_text().splitlines()
        if not 0 <= index < len(lines):
            return
        lines[index] = render(str(task))
        self.queue_path.write_text("\n".join(lines) + "\n")


def _title(task: str) -> str:
    """
    Name a session by what it is doing, not by the vendor running it.

    The vendor is an implementation detail that may differ between iterations
    of the same task; the task is what you will be looking for in the sidebar.

    :param task: The task line.
    :returns: A short session title.
    """
    words = task.strip().split()
    return "-".join(words[:6]).lower().strip("-") or "task"
