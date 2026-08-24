"""A bot that watches a page and reports when it changes.

The research workload reads a repository. This one reads the web, which is a
different problem in exactly one way that matters: **the page is not yours**.
It can lie, it can be replaced, and anything on it that looks like an
instruction is content somebody else wrote. So the brief says so in as many
words, and the only thing this workload trusts from a page is that the agent
looked at it.

## Why a bot should browse rather than fetch

Half the pages worth watching are behind a login, a consent wall, or JavaScript
that a `curl` never runs. The agent drives the Omnigent desktop app's embedded
browser through the ``browser_*`` relay — a real Chromium page, the same one a
person watching the Bots page can see and, when they take the wheel, drive
themselves. That shared surface is the point: a screenshot of a browser nobody
can touch proves nothing.

## The baseline is the whole feature

A watcher with no memory reports "the page says 0.40%" every morning, which is
noise. This one keeps the last observation in the bot's workspace and asks the
agent to compare, so a wake with nothing new is ``NO_WORK`` and costs a backoff
rather than a person's attention.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from army.bots.workloads.browsing import browsing_rules
from army.omni import OmniClient, OmniError
from army.state import Run

_logger = logging.getLogger(__name__)

#: Kept small on purpose. A watcher's answer is "changed / did not change, and
#: here is the number" — anything longer is the page pasted into the channel.
_REPLY_CHARS = 3_000


class WatchWorkload:
    """Watch one page and report only when the answer changes.

    :param url: What to watch.
    :param question: What to answer about it, in one sentence.
    :param state: Where the last observation is kept, so a wake with nothing
        new is cheap. Relative paths hang off the bot's workspace.
    :param agent: The Omnigent agent to run as.
    :param host: The Omnigent host the body runs on.
    :param workspace: The bot's directory; reports land in ``reports/``.
    :param charter: Prepended to the brief, absolute or workspace-relative.

    Names no browser. A bot has exactly one, ``bot.browser_profile``, and the
    supervisor labels every session with it — its own, never shared, because
    one profile across bots means one bot's compromise is every bot's session
    and an audit trail that cannot say which bot did a thing.
    """

    name = "watch"

    def __init__(
        self,
        url: str,
        question: str,
        *,
        state: str = "last-seen.json",
        agent: str = "claude-native-ui",
        host: str | None = None,
        workspace: str = ".",
        charter: str | None = None,
    ) -> None:
        self.url = url
        self.question = question
        self.workspace = Path(workspace).expanduser()
        self.state = self._resolve(state)
        self.agent = agent
        self.host = host
        self.charter = self._resolve(charter) if charter else None

    def _resolve(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        return candidate if candidate.is_absolute() else self.workspace / candidate

    # ── the five methods ──────────────────────────────────────────

    def acquire(self) -> dict[str, Any] | None:
        """
        Always something to do — the wake policy decides how often.

        A watcher has no queue. Its precondition is the clock, which is what
        ``rrule`` is for; returning an item every time is correct here and
        would be wrong for the research workload, whose queue can be empty.
        """
        return {"url": self.url, "question": self.question, "last": self._last()}

    def dispatch(self, run: Run, omni: OmniClient) -> list[str]:
        """
        Open a session and point it at the page.

        :param run: The run, in ``DISPATCHING``.
        :param omni: The Omnigent client.
        :returns: The session id.
        """
        # The server refuses to open a session on a directory that is not
        # there, and the refusal names the host rather than the bot — so a
        # fresh clone of an example fails with "workspace path does not exist
        # on host" one layer below anything the operator wrote. This workload
        # already creates `reports/` and its own state file inside here; the
        # directory itself is no different.
        self.workspace.mkdir(parents=True, exist_ok=True)

        # No browser label here. The supervisor puts the bot's own profile on
        # every session it opens, so this workload cannot get it wrong and the
        # next one cannot forget it — which is exactly what happened to the
        # research workload, leaving nine of ten bots unable to browse with
        # nothing anywhere saying so.
        session = omni.create_session(
            omni.resolve_agent(self.agent),
            title=f"watch: {self.url[:60]}",
            workspace=str(self.workspace),
            host_id=self.host,
        )
        omni.send(session, self._brief(run))
        return [session]

    def collect(self, run: Run, omni: OmniClient) -> tuple[bool, dict[str, Any]]:
        """
        Wait for the look, and keep a bounded receipt of what was seen.

        :param run: The run, in ``COLLECTING``.
        :param omni: The Omnigent client.
        :returns: ``(done, artifacts)``.
        """
        sessions = run.artifacts.get("sessions") or run.outstanding
        if not sessions:
            return True, {"reply": "", "note": "no session was opened"}
        try:
            snapshot = omni.get_session(sessions[0])
        except OmniError as exc:
            _logger.warning("run %s: could not read the session (%s)", run.id[:12], exc)
            return False, {}
        if snapshot.status not in ("idle", "completed", "failed"):
            return False, {}
        # The *last* thing it said, not everything it said. An agent narrates
        # as it works — "I'll load the browser tools and check the page" — and
        # joining the lot puts that narration on line one, where the verdict is
        # supposed to be. One run read its own opening sentence as the verdict
        # and filed a page whose fee had moved as nothing to report.
        said = omni.agent_said(sessions[0])
        reply = said[-1][-_REPLY_CHARS:] if said else ""
        return True, {"reply": reply, "session_status": snapshot.status}

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:
        """
        Decide whether this was news, and record the new baseline.

        The agent is asked to open its reply with ``CHANGED`` or ``UNCHANGED``
        on its own line, because a watcher that needs a model to interpret its
        own output has moved the judgement to the wrong side of the seam.

        :param run: The run, in ``EVALUATING``.
        :returns: ``(question, options, evidence)``.
        """
        reply = str(run.artifacts.get("reply") or "").strip()
        headline = reply.split("\n", 1)[0].strip() if reply else ""
        # The first *word*, not the line with its punctuation removed. Models
        # write "**CHANGED**", "### UNCHANGED", "CHANGED:" — so the markdown
        # has to go. But stripping everything and matching a prefix read
        # "CHANGED — the page now shows a login wall" as CHANGED, which wrote
        # the login wall in as the baseline: every later iteration compared
        # against it, reported UNCHANGED, and the watcher went quietly blind.
        found = re.match(r"[^A-Za-z]*([A-Za-z]+)", headline)
        verdict = found.group(1).upper() if found else ""

        evidence = {
            "url": self.url,
            "watching for": self.question,
            "previously": str(run.payload.get("last") or "nothing recorded yet"),
            "verdict": headline or "the agent did not say",
        }

        # A wall the bot must not climb. It is asked to stop rather than to
        # find a credential, and the answer is a person taking the wheel of
        # this very browser and signing in themselves — which is the one thing
        # a shared browser makes possible and a screenshot-beside-a-fetch
        # cannot. No baseline is written: nothing was read.
        if verdict == "LOGIN":
            evidence["needs"] = "somebody to sign in on this browser"
            return (
                f"{self.url} wants a sign-in",
                ["I signed in — look again", "stop"],
                evidence,
            )

        if verdict == "CHANGED":
            self._remember(reply)
            report = self._write_report(run, reply)
            if report:
                evidence["report"] = report.name
            return (f"{self.url} changed", ["acknowledge", "stop"], evidence)

        if verdict == "UNCHANGED":
            # Nothing new. Asking a person to confirm that is how a watcher
            # trains them to ignore it.
            return ("", [], evidence)

        # Anything else is an answer nobody can act on. Reading it as no-news
        # is the dangerous default: a watcher that cannot parse its own agent
        # goes silent, and silence is what it says when the page has not
        # moved — so a broken watcher is indistinguishable from a calm one.
        return (
            f"could not tell whether {self.url} changed",
            ["look again", "stop"],
            {**evidence, "unparsed": headline or "(the agent said nothing)"},
        )

    def apply(
        self,
        run: Run,  # noqa: ARG002 — protocol shape; a watcher needs only the choice
        decision: str,
        payload: dict[str, Any],
    ) -> tuple[str, str]:
        """
        Acknowledge the change, or stand the watcher down.

        :param run: The run, in ``WAITING_HUMAN``.
        :param decision: The command that arrived.
        :param payload: What it carried.
        :returns: ``(next_state, reason)``.
        """
        if decision != "approve" or payload.get("choice") == "stop":
            return "paused", "stood down by the operator"
        # "I signed in" is not an acknowledgement of anything — the page was
        # never read. Going round again is the point, and the browser profile
        # is persistent, so the session the person just established is the one
        # the next iteration opens on.
        if str(payload.get("choice", "")).startswith("I signed in"):
            return "continue", "signed in by a person; looking again"
        return "continue", "change acknowledged"

    # ── the baseline ──────────────────────────────────────────────

    def _last(self) -> str | None:
        """The previous observation, or ``None`` on the first ever wake."""
        try:
            return str(json.loads(self.state.read_text()).get("summary"))
        except (OSError, ValueError, AttributeError):
            return None

    def _remember(self, reply: str) -> None:
        """
        Write the new baseline, keeping the one it replaces.

        The baseline is the whole feature and also the whole failure mode: a
        wrong one makes every later iteration report UNCHANGED, and a watcher
        that has gone blind says exactly what a watcher with nothing to report
        says. It cannot be detected from inside — an agent that leads with
        CHANGED and then explains it could not read the page has still said
        CHANGED — so the defence is that the previous baseline survives in the
        bot's own workspace, where the Files panel shows it and a person can
        see what it was replaced with.
        """
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(
            json.dumps(
                {
                    "url": self.url,
                    "seen_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "summary": reply[:1_000],
                    "previously": self._last(),
                },
                indent=2,
            )
            + "\n"
        )

    def _write_report(self, run: Run, reply: str) -> Path | None:
        """Keep the agent's own words, so a change has a durable record."""
        reports = self.workspace / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M")
        path = reports / f"{stamp}-watch.md"
        path.write_text(f"# {self.url}\n\nRun `{run.id[:12]}` · {stamp}Z\n\n{reply}\n")
        return path

    def _brief(self, run: Run) -> str:
        """The charter, the page, the baseline, and the rules about pages."""
        charter = ""
        if self.charter and self.charter.exists():
            charter = self.charter.read_text().strip() + "\n\n---\n\n"
        last = run.payload.get("last")
        baseline = (
            f"## What you reported last time\n\n{last}\n\n"
            "Compare against it. If the answer is materially the same, say so "
            "and stop — a watcher that reports no news every morning is a "
            "watcher people stop reading.\n\n"
            if last
            else "## This is the first look\n\nThere is no baseline yet, so "
            "record what you find and treat it as CHANGED.\n\n"
        )
        return (
            f"{charter}"
            f"# Watch\n\n**Page:** {self.url}\n\n**Question:** {self.question}\n\n"
            f"{baseline}"
            f"{browsing_rules(verdict_word='LOGIN')}\n\n"
            "## Your reply\n\n"
            "Open with **CHANGED**, **UNCHANGED** or **LOGIN** on a line of "
            "its own. Then the number or fact you were asked for, then how you "
            "know — the URL and what the page actually said. Keep it short.\n\n"
            f"Run id `{run.id[:12]}`.\n"
        )
