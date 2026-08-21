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
    :param implementer_agent: Agent id for the vendor that writes the change.
    :param reviewer_agent: Agent id for the vendor that judges it. Point this
        at a different vendor than the implementer.
    :param workspace: Directory the sessions run in.
    """

    name = "demo"

    def __init__(
        self,
        queue_path: str = "army-queue.txt",
        implementer_agent: str = "implementer",
        reviewer_agent: str = "reviewer",
        workspace: str | None = None,
        host_id: str | None = None,
    ) -> None:
        self.queue_path = Path(queue_path).expanduser()
        self.implementer_agent = implementer_agent
        self.reviewer_agent = reviewer_agent
        self.workspace = workspace
        self.host_id = host_id
        # Resolved once per process, on first use. The names in army.toml are
        # what a person writes; the API wants the ids it minted.
        self._agent_ids: dict[str, str] = {}

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
        """Take the first unfinished line from the queue file."""
        if not self.queue_path.exists():
            return None
        lines = self.queue_path.read_text().splitlines()
        for index, line in enumerate(lines):
            task = line.strip()
            if not task or task.startswith(("#", "done:")):
                continue
            lines[index] = f"done: {task}"
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
        if session_id is not None:
            return [session_id]
        session_id = omni.create_session(
            self._agent_id(self.implementer_agent, omni),
            title=title,
            workspace=self.workspace,
            host_id=self._host(omni),
        )
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
            ["merge", "iterate", "discard", "stop"],
            evidence,
        )

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:
        """Turn the answer into the loop's next move.

        A decline pauses this branch and nothing else. It never falls through
        to an approval — a gate that approves when unanswered is not a gate.
        """
        if decision == "deny":
            self._requeue(run)
            return "paused", "declined; task returned to the queue"

        choice = str(payload.get("choice") or "merge")
        if choice == "stop":
            return "completed", "owner stopped the loop"
        if choice == "discard":
            return "continue", "change discarded; moving on"
        if choice == "iterate":
            self._requeue(run)
            return "continue", "task returned to the queue for another pass"
        return "continue", "approved"

    # ── queue bookkeeping ─────────────────────────────────────────

    def _requeue(self, run: Run) -> None:
        """Put a task back so the next iteration picks it up again."""
        task = run.payload.get("task")
        if not task or not self.queue_path.exists():
            return
        lines = self.queue_path.read_text().splitlines()
        index = run.payload.get("line")
        if isinstance(index, int) and 0 <= index < len(lines):
            lines[index] = str(task)
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
