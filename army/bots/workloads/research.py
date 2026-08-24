"""One research iteration in a repository: claim a question, answer it, report.

The engine knows about runs and wakes; this knows about a queue file, a git
worktree and a report. Nothing here is about crypto, trading or fees — those
live in the bot's charter, its queue and its YAML, which is the whole point of
the workload seam. Pointed at a different repository with a different queue it
is a literature-review bot or a dependency-audit bot without a line changing.

## What one iteration is

Claim the first unclaimed line of the queue, hand it to an agent in the bot's
own worktree along with its charter, wait for the agent to finish, then check
what changed on disk and write it up. The agent does the thinking; this decides
what it was allowed to touch and whether the result is worth keeping.

## The part that is not the agent's decision

``DENIED_PATHS`` is checked after the session ends, against ``git status``, and
a match refuses the commit and blocks the run. It is deliberately not a
sentence in the charter: a prompt is a request, and the paths on that list are
the ones where a request is not good enough — an owner channel a bot may not
write to is not an owner channel, and a deploy gate a bot may edit is not a
gate.

A refusal here is ``BLOCKED``, not a retry. A bot that touched a denied path
did something nobody asked for, and the correct next step is a person reading
the diff, not the same prompt again.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from army.omni import OmniClient, OmniError
from army.state import Run

_logger = logging.getLogger(__name__)

#: Paths a research bot may never modify, checked against ``git status`` after
#: the agent has finished rather than asked for in the prompt.
#:
#: Each of these is somewhere a write would be self-authorisation rather than
#: work: the owner's inbound channel, the roster that says which agents exist,
#: the gate that arms real money, the trading configs, and the automation that
#: would make any of it survive a reboot.
DENIED_PATHS: tuple[str, ...] = (
    "hitl/",
    "registry/",
    "tools/deploy_gate/",
    "policies/",
    "configs/",
    ".github/",
    "Makefile",
)

#: How much of the agent's reply is kept. A backtest's stdout is megabytes and
#: the run record is read by a person; the report the agent writes to disk is
#: the artifact, this is the receipt.
_REPLY_CHARS = 4_000

#: A queue line that has been taken. Written back in place so a crash between
#: claiming and finishing does not hand the same question to a second run.
_TAKEN = "taken:"

_DONE = "done:"

_SLUG = re.compile(r"[^a-z0-9]+")


class ResearchWorkload:
    """A repository research loop, driven by a plain-text queue.

    :param repo: The bot's own worktree. Never the operator's checkout — the
        supervisor's workspace layer makes the worktree and refuses if it
        cannot, so by the time this runs the isolation was delivered.
    :param queue: Questions to work through, one per line. Absolute, or
        relative to *repo*. Keep it in the bot's own workspace: the queue is
        the bot's bookkeeping, and a repository should receive work product
        rather than a record of what its tools were doing.
    :param reports: Where write-ups go, relative to *repo*. This one *is* work
        product, so it belongs in the branch.
    :param agent: The Omnigent agent to run as.
    :param host: The Omnigent host to run on. Required in practice: a session
        created without one has nowhere to start a runner, and the failure
        surfaces as ``runner_failed_to_start`` several minutes later rather
        than as "you did not say where".
    :param charter: The bot's charter — absolute, or relative to *repo*. One
        file. A charter copied into the repository is a charter that drifts
        from the one the operator edits.
    :param denied: Paths it may not modify. Defaults to :data:`DENIED_PATHS`.
    :param commit: Whether to commit the result to the worktree's branch.
    """

    name = "research"

    def __init__(
        self,
        repo: str,
        *,
        queue: str = "research-queue.md",
        reports: str = "reports",
        agent: str = "claude-native-ui",
        host: str | None = None,
        charter: str | None = None,
        denied: tuple[str, ...] = DENIED_PATHS,
        commit: bool = True,
    ) -> None:
        self.repo = Path(repo).expanduser()
        self.queue = self._resolve(queue)
        self.reports = self.repo / reports
        self.agent = agent
        self.host = host
        self.charter = self._resolve(charter) if charter else None
        self.denied = denied
        self.commit = commit

    def _resolve(self, path: str) -> Path:
        """Absolute stays put; relative hangs off the repository."""
        candidate = Path(path).expanduser()
        return candidate if candidate.is_absolute() else self.repo / candidate

    # ── the five methods ──────────────────────────────────────────

    def acquire(self) -> dict[str, Any] | None:
        """
        Claim the first unclaimed question.

        Marked ``taken:`` in place before the run starts, so a crash between
        here and the report leaves a line a person can see was in flight rather
        than one a second run picks up again.

        :returns: The item, or ``None`` when the queue is empty — which is a
            normal night, not a failure.
        """
        if not self.queue.exists():
            return None
        lines = self.queue.read_text().splitlines()
        for index, line in enumerate(lines):
            question = line.strip()
            if not question or question.startswith(("#", _TAKEN, _DONE)):
                continue
            lines[index] = f"{_TAKEN} {question}"
            self.queue.write_text("\n".join(lines) + "\n")
            return {"question": question, "line": index}
        return None

    def brief_extra(self, said: list[str]) -> str:
        """
        What the operator said since the last iteration, for the next brief.

        The other half of steering. A message to a *busy* bot is forwarded into
        its session; a message to an idle one has nowhere to go, and would be
        read weeks later as channel history if it were not put in front of the
        next body deliberately.

        Placed above the question, because a correction that arrives after the
        instruction it corrects is a correction nobody applies.

        :param said: The messages, oldest first.
        :returns: A markdown block, or an empty string.
        """
        if not said:
            return ""
        lines = "\n".join(f"- {line.strip()}" for line in said if line.strip())
        return (
            "## The operator said this since your last iteration\n\n"
            f"{lines}\n\n"
            "Read it before the question below; it may change what the right "
            "answer is, or make the question moot. If it does, say so rather "
            "than answering the question anyway. None of it is an approval.\n\n"
            "---\n\n"
        )

    def dispatch(self, run: Run, omni: OmniClient) -> list[str]:
        """
        Open one session in the worktree and give it the question.

        :param run: The run, in ``DISPATCHING``.
        :param omni: The Omnigent client.
        :returns: The session id, so the engine can collect from it.
        """
        question = str(run.payload.get("question", ""))
        said = [str(line) for line in run.payload.get("said") or []]
        session = omni.create_session(
            omni.resolve_agent(self.agent),
            title=f"research: {question[:60]}",
            workspace=str(self.repo),
            host_id=self.host,
        )
        omni.send(session, self.brief_extra(said) + self._brief(question, run))
        return [session]

    def collect(self, run: Run, omni: OmniClient) -> tuple[bool, dict[str, Any]]:
        """
        Wait for the agent, then keep a bounded receipt of what it said.

        :param run: The run, in ``COLLECTING``.
        :param omni: The Omnigent client.
        :returns: ``(done, artifacts)``.
        """
        sessions = run.artifacts.get("sessions") or run.outstanding
        if not sessions:
            return True, {"reply": "", "note": "no session was opened"}
        session = sessions[0]
        try:
            snapshot = omni.get_session(session)
        except OmniError as exc:
            # Transient by default: the engine's own retry policy decides, and
            # a collect that raises would fail an iteration whose agent may
            # still be working.
            _logger.warning("run %s: could not read session %s (%s)", run.id[:12], session, exc)
            return False, {}
        if snapshot.status not in ("idle", "completed", "failed"):
            return False, {}

        replies = omni.agent_said(session)
        reply = "\n".join(replies)[-_REPLY_CHARS:]
        return True, {"reply": reply, "session_status": snapshot.status}

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:
        """
        Check what changed on disk, write the report, and ask whether to go on.

        The denied-path check happens here rather than in the prompt, and its
        result is evidence rather than prose: an operator answering this should
        be able to see that nothing outside the research area moved without
        taking anyone's word for it.

        :param run: The run, in ``EVALUATING``.
        :returns: ``(question, options, evidence)``.
        """
        question = str(run.payload.get("question", ""))
        changed = self._changed_paths()
        trespass = sorted({path for path in changed if self._is_denied(path)})

        evidence: dict[str, Any] = {
            "question": question,
            "files changed": str(len(changed)),
            "denied paths touched": ", ".join(trespass) if trespass else "none",
            "branch": self._branch(),
        }

        if trespass:
            # Nothing is committed and nothing is asked. A bot that wrote to
            # the owner channel or the deploy gate is not a bot whose next
            # question matters.
            return (
                f"{run.payload.get('question', 'this iteration')} modified paths it may not touch",
                ["stop"],
                {**evidence, "refused": "the working tree was left for you to inspect"},
            )

        report = self._write_report(run, question)
        evidence["report"] = str(report.relative_to(self.repo)) if report else "none"
        if self.commit:
            evidence["commit"] = self._commit(question) or "nothing to commit"

        return (
            f"Researched: {question}",
            ["continue", "stop"],
            evidence,
        )

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:
        """
        Mark the queue line and decide whether the bot keeps going.

        :param run: The run, in ``WAITING_HUMAN``.
        :param decision: The command that arrived.
        :param payload: What it carried.
        :returns: ``(next_state, reason)``.
        """
        if decision != "approve" or payload.get("choice") == "stop":
            return "paused", "stopped by the operator"
        self._close_line(run)
        return "continue", "research recorded"

    # ── the parts that are not the agent's decision ───────────────

    def _is_denied(self, path: str) -> bool:
        """Whether a changed path is one no research iteration may modify."""
        return any(path == entry or path.startswith(entry) for entry in self.denied)

    def _changed_paths(self) -> list[str]:
        """Every path git reports as modified, added or deleted in the worktree."""
        result = self._git("status", "--porcelain")
        paths = []
        for line in (result or "").splitlines():
            if len(line) > 3:
                # Rename lines read `R  old -> new`; the destination is what
                # was written, and it is the one that matters here.
                paths.append(line[3:].split(" -> ")[-1].strip())
        return paths

    def _branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"

    def _write_report(self, run: Run, question: str) -> Path | None:
        """
        Keep the agent's own words next to the question that prompted them.

        The agent is asked to write its own report to `reports/`; this is the
        fallback for an iteration that answered in chat and wrote nothing, so
        an operator is never left with a run that says "work done" and an empty
        directory.
        """
        reply = str(run.artifacts.get("reply") or "").strip()
        if not reply:
            return None
        self.reports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M")
        slug = _SLUG.sub("-", question.lower()).strip("-")[:60] or "iteration"
        path = self.reports / f"{stamp}-{slug}.md"
        if path.exists():
            return path
        path.write_text(
            f"# {question}\n\n"
            f"Run `{run.id[:12]}` · branch `{self._branch()}` · {stamp}Z\n\n"
            f"{reply}\n"
        )
        return path

    def _commit(self, question: str) -> str | None:
        """
        Commit to the worktree's own branch. Never pushes.

        No remote is contacted and none is configured for this to use: a
        research bot that can push is a research bot that can put its own work
        in front of a reviewer without one.
        """
        self._git("add", "-A")
        if not self._changed_paths() and not self._git("diff", "--cached", "--name-only"):
            return None
        subject = f"research: {question}"[:72]
        self._git("commit", "-m", subject, "--no-verify")
        return self._git("rev-parse", "--short", "HEAD")

    def _close_line(self, run: Run) -> None:
        """Turn the claimed line into a finished one."""
        index = run.payload.get("line")
        if not self.queue.exists() or not isinstance(index, int):
            return
        lines = self.queue.read_text().splitlines()
        if 0 <= index < len(lines) and lines[index].startswith(_TAKEN):
            lines[index] = lines[index].replace(_TAKEN, _DONE, 1)
            self.queue.write_text("\n".join(lines) + "\n")

    def _brief(self, question: str, run: Run) -> str:
        """The charter, the question, and the rules that are not negotiable."""
        charter = ""
        if self.charter and self.charter.exists():
            charter = self.charter.read_text().strip() + "\n\n---\n\n"
        denied = "\n".join(f"- {entry}" for entry in self.denied)
        return (
            f"{charter}"
            f"# This iteration\n\n{question}\n\n"
            f"You are working in `{self.repo}`, which is a git worktree on branch "
            f"`{self._branch()}`. It is yours; the operator's checkout is elsewhere.\n\n"
            f"## What to produce\n\n"
            f"Write your findings to `{self.reports.relative_to(self.repo)}/` as markdown — "
            f"what you tried, what the "
            f"numbers were, and whether the answer is yes or no. **A confident no with "
            f"evidence is a complete result**; do not manufacture a positive.\n\n"
            f"## Paths you must not modify\n\n{denied}\n\n"
            f"These are checked against `git status` after you finish, outside this "
            f"conversation. Touching one refuses the whole iteration.\n\n"
            f"Do not run `git push`, `make persist`, or anything that arms live trading.\n\n"
            f"Run id `{run.id[:12]}`.\n"
        )

    def _git(self, *args: str) -> str | None:
        """Run git in the worktree, returning stdout or ``None`` on failure."""
        result = subprocess.run(
            ["git", *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            _logger.warning("git %s failed: %s", args[0], result.stderr.strip()[:200])
            return None
        return result.stdout.strip()
