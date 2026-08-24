"""The Bots surface, served by this process rather than only by the app.

Omnigent's web app has a Bots section (``omnigent/server/routes/bots.py``
forwards to the JSON here). This page is the other way in: it needs no Omnigent
server, it is what that proxy talks to, and it opens on a phone over the
tailnet — which is the whole reason the approval path had to be answerable from
one.

No framework, no build step, no new dependency: :mod:`http.server` and a string.
The design tokens are read from Omnigent's own ``index.css`` so the page looks
like the product rather than like a tool bolted to it.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import re
import secrets
import time
from contextlib import suppress
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from army.bots.approvals import ApprovalRefused, ApprovalStore
from army.bots.budget import BudgetExhausted
from army.bots.messages import MessageKind, MessageStore
from army.bots.model import MAX_DEPTH, MAX_FANOUT, BotStatus, DerivedStatus
from army.bots.roster import RosterEntry, roster, summarise
from army.bots.schedule import first_wake
from army.bots.spawn import SpawnRefused
from army.bots.store import BotStore
from army.bots.wheel import REFUSAL
from army.bots.workspace import Workspace
from army.gates import GateRefused, digest
from army.store import ConcurrentTransition, Store

_logger = logging.getLogger(__name__)

#: Where the page's shared secret lives. Deliberately inside the control-plane
#: directory, which :data:`army.bots.isolation.CONTROL_PLANE_DIRS` already
#: withholds from every bot's sandbox — so the one thing a bot cannot read is
#: the one thing that authorises a decision.
TOKEN_FILE = Path.home() / ".omnigent" / "army" / "web-token"

#: Exactly the shape of an id this codebase mints. Matching loosely let a
#: caller send ``00%`` and have a prefix search decide which approval they
#: meant, which is a way to answer questions you were never shown.
_ID = re.compile(r"^[0-9a-f]{32}$")

#: How much of a file the viewer will fetch. A bot's charter is a page; a
#: report is a few. Anything larger is a log, and streaming a log through a
#: JSON field into a React state is how a browser tab dies.
MAX_READ_BYTES = 512_000

#: How long a message to a bot may be. Long enough for a paragraph of
#: correction, short enough that the channel does not become the place someone
#: pastes a log — the workspace is for that.
MAX_MESSAGE_CHARS = 4_000


def read_or_mint_token(path: Path = TOKEN_FILE) -> str:
    """
    Load the page's secret, creating one on first use.

    Written ``0600`` under a directory bots cannot read. The alternative —
    treating "can reach the port" as authority — is not a weaker
    authentication scheme, it is none: the bots are *on* the network, so a
    bot's own shell can read the pending approvals and answer them, and the
    resulting verdict is indistinguishable from the operator's.

    :param path: Where the secret lives.
    :returns: The secret.
    """
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    minted = secrets.token_urlsafe(32)
    # Written before the mode is set on some platforms, so create it closed.
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(minted + "\n")
    _logger.info("minted a bots-page token at %s", path)
    return minted


#: Every value is read out of ``web/src/index.css`` or a real component, so the
#: page is the product's own vocabulary rather than an approximation of it.
#: Two body steps, because that is all Omnigent ships — ``text-xs`` is retired
#: upstream and aliased to ``text-sm`` on purpose.
_STYLE = """
:root{
  --card:#fff; --canvas:#fdfafa; --sidebar:#fdfafa;
  --foreground:#11171c; --muted-foreground:#71717a;
  --border:#e4e4e7; --border-weak:#ececee;
  --sidebar-active:rgba(240,1,150,.1); --sidebar-active-foreground:#651249;
  --status-green:#2ea65c; --status-yellow:#d4972a; --status-red:#f04858;
  --status-gray:#8e99a4; --accent-foreground:#04355d; --ring:#11171c;
  --text-ui:13px; --text-sm:11.7px; --lh:1.6;
  --radius-button:6px; --radius-sm:8px;
  --font-sans:ui-sans-serif,system-ui,sans-serif;
  --font-mono:"Geist Mono Variable","JetBrains Mono",ui-monospace,monospace;
}
*{box-sizing:border-box}
body{margin:0;font-family:var(--font-sans);color:var(--foreground);
  background:var(--canvas);font-size:var(--text-ui);line-height:var(--lh);
  -webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
::selection{background:var(--sidebar-active);color:var(--sidebar-active-foreground)}
:focus-visible{outline:2px solid var(--ring);outline-offset:2px;border-radius:4px}
a{color:var(--accent-foreground)}
h1,h2{margin:0;font-size:var(--text-ui);font-weight:500}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 64px}
.top{display:flex;align-items:baseline;justify-content:space-between;padding-bottom:14px}
.wordmark{font-size:17px;font-weight:700;letter-spacing:-.01em}
.sm{font-size:var(--text-sm)}
.mut{color:var(--muted-foreground)}
.mono{font-family:var(--font-mono)}
.grp{margin:22px 0 6px;font-size:var(--text-sm);color:var(--muted-foreground)}
/* One card species, borrowed from the Alert primitive: uniform hairline, no
   tint, no shadow, and never a coloured border on one side. */
.card{background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius-sm);padding:8px 10px;margin-bottom:8px}
.row{display:grid;grid-template-columns:8px 1fr auto;column-gap:10px;align-items:start}
.dot{width:8px;height:8px;border-radius:50%;margin-top:7px}
.act{background:var(--status-red)} .run{background:var(--status-green)}
.hold{background:var(--status-yellow)} .idle{border:1.5px solid var(--status-gray)}
.name{font-weight:500}
.when{font-family:var(--font-mono);font-size:var(--text-sm);color:var(--muted-foreground);
  white-space:nowrap}
.why{display:block;font-size:var(--text-sm);color:var(--muted-foreground)}
.acts{display:flex;gap:6px;margin-top:9px;flex-wrap:wrap}
.btn{padding:3px 10px;border-radius:var(--radius-button);border:1px solid #d6d6d6;
  background:var(--card);font:inherit;line-height:20px;cursor:pointer}
.btn:hover{background:#0000000f}
.btn.primary{background:#11171c;color:#fff;border-color:#11171c}
.foot{margin-top:8px;font-family:var(--font-mono);font-size:var(--text-sm);
  color:var(--muted-foreground)}
.empty{padding:18px 10px;color:var(--muted-foreground)}
.err{border-color:var(--status-red)}
table{border-collapse:collapse;width:100%}
td{padding:5px 8px 5px 0;vertical-align:top;border-top:1px solid var(--border-weak)}
.defn{margin:8px 0 0;padding:8px 10px;background:#0000000a;border-radius:var(--radius-button);
  font-family:var(--font-mono);font-size:var(--text-sm);white-space:pre-wrap;overflow-x:auto}
"""

#: Which disc a derived status gets. Colour lives in an 8px disc and nowhere
#: else — never behind type, never as a border on one edge of a card.
_DISC: dict[DerivedStatus, str] = {
    DerivedStatus.WAITING_HUMAN: "act",
    DerivedStatus.BLOCKED: "act",
    DerivedStatus.RUNNING: "run",
    DerivedStatus.WAITING_RESOURCE: "hold",
    DerivedStatus.BACKING_OFF: "hold",
}


def _bare_profile(profile: str) -> str:
    """
    A browser profile without its ``persist:`` prefix.

    Both spellings name one browser: definitions write the Electron partition
    form, and a hand-written one often writes the bare slug. Comparing them
    literally means a held wheel silently refuses nothing — which is the same
    shape as no wheel at all, and reads as a bot ignoring the handoff.

    :param profile: Either spelling.
    :returns: The bare name.
    """
    return profile.removeprefix("persist:").strip()


class BotsSite:
    """Renders the fleet, and accepts a verdict.

    :param store: The run store.
    :param bots: The bot store.
    :param approvals: The approval ledger.
    :param messages: The channel, for the verdict trail.
    :param token: The shared secret every request must carry. Not a login —
        it authenticates *reaching a file bots cannot read*, which is the
        property that matters here.
    :param spawns: Proposals from bots, or ``None`` to hide that page.
    :param budgets: The ledger, so the roster can say what is left.
    :param wheels: Who is driving each bot's browser, or ``None`` to disable
        control handoff entirely.
    :param workspace: Resolves where a bot's directory is. Needed because
        ``bots.workspace`` is only set when a definition named one — a bot that
        took the default root has the column empty, and reading the column
        directly would report "no workspace" for a bot that plainly has one.
    """

    def __init__(
        self,
        store: Store,
        bots: BotStore,
        approvals: ApprovalStore,
        messages: MessageStore,
        token: str,
        spawns: Any = None,
        budgets: Any = None,
        workspace: Workspace | None = None,
        wheels: Any = None,
    ) -> None:
        self.store = store
        self.bots = bots
        self.approvals = approvals
        self.messages = messages
        self.token = token
        self.spawns = spawns
        self.budgets = budgets
        self.workspace = workspace if workspace is not None else Workspace(bots)
        self.wheels = wheels

    def authorises(self, supplied: str) -> bool:
        """
        Whether a request carried the secret.

        Compared in constant time, because the token is short enough that a
        byte-at-a-time comparison is a genuine oracle over a fast local link.

        :param supplied: What the caller sent.
        :returns: Whether it matches.
        """
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    # ── pages ─────────────────────────────────────────────────────

    def index(self, *, now: int, notice: str = "", problem: str = "") -> str:
        """
        The roster, with what needs a person at the top.

        :param now: Epoch seconds.
        :param notice: A confirmation to show once.
        :param problem: A refusal to show once.
        :returns: An HTML document.
        """
        entries = roster(self.bots, now=now)
        pending = self.approvals.pending()
        counts = summarise(entries)

        body = [
            '<div class="top"><span class="wordmark">Omnigent</span>',
            f'<span class="sm mut">{_counts(counts)}</span></div>',
            "<h1>Bots</h1>",
            f'<p class="grp"><a href="/proposals?token={_q(self.token)}">proposed bots</a>'
            f' · <a href="/api/bots?token={_q(self.token)}">json</a></p>',
        ]
        if problem:
            body.append(f'<div class="card err"><strong>Refused.</strong> {_esc(problem)}</div>')
        if notice:
            body.append(f'<div class="card">{_esc(notice)}</div>')

        body.append('<p class="grp" id="g-decide">Needs you</p>')
        if not pending:
            body.append('<div class="empty">Nothing is waiting on you.</div>')
        for request in pending:
            body.append(self._approval_card(request, now=now))

        body.append('<p class="grp" id="g-fleet">Fleet</p>')
        if not entries:
            body.append(
                '<div class="empty">No bots yet. '
                '<span class="mono sm">army bots example &gt; bot.yaml</span></div>'
            )
        for entry in entries:
            body.append(self._bot_row(entry, now=now))

        return _page("Bots", "".join(body))

    def _approval_card(self, request: Any, now: int) -> str:
        """
        One question, what it is actually bound to, and the buttons.

        The question and the options are written by the bot. The verdict is
        bound to the *verb and evidence*, which the bot also wrote but which at
        least describe what will happen rather than what it would like you to
        think. Showing only the prose meant a human could be bound to a hash of
        arguments they were never shown — "spend $5 on a domain" over a
        five-figure transfer, and the digest still matches.
        """
        bot = self.bots.get(request.bot_id)
        name = bot.slug if bot else request.bot_id[:12]
        owner = (
            '<span class="sm mut"> · owner-only, answer from the signed channel</span>'
            if request.requires_owner
            else ""
        )
        # Each option posts `decision=approve` alongside its own value, and
        # the deny button posts `decision=deny`. The bot authors the option
        # labels, so the *label* may say anything; what it does is fixed here.
        #
        # An owner-only verb gets no approve button at all. Rendering one that
        # is guaranteed to be refused teaches the operator that the buttons are
        # advisory, which is the opposite of what this page is for.
        options = (
            ""
            if request.requires_owner
            else "".join(
                f'<button class="btn{" primary" if index == 0 else ""}" name="choice"'
                f' value="{_esc(option)}" formaction="/verdict?decision=approve">'
                f"{_esc(option)}</button>"
                for index, option in enumerate(request.options)
            )
        )
        left = ""
        if request.expires_at is not None:
            left = f" · expires in {_short(request.expires_at - now)}"
        # What the verdict is bound to, rather than what the bot said about it.
        bound = "".join(
            f'<tr><td class="sm mut">{_esc(key)}</td><td class="mono sm">{_esc(value)}</td></tr>'
            for key, value in sorted((request.evidence or {}).items())
        )
        detail = (
            f'<table>{bound}<tr><td class="sm mut">verb</td>'
            f'<td class="mono sm">{_esc(request.verb)}</td></tr></table>'
        )
        return (
            '<div class="card"><div class="row"><span class="dot act"></span>'
            f'<span><span class="name">{_esc(name)}</span>{owner}'
            f'<span class="why">{_esc(request.question)}</span></span>'
            f'<span class="when">{_short(now - request.created_at)} ago</span></div>'
            f"{detail}"
            f'<form method="post" action="/verdict" class="acts">'
            f'<input type="hidden" name="approval" value="{_esc(request.id)}">'
            f'<input type="hidden" name="token" value="{_esc(self.token)}">'
            f"{options}"
            '<button class="btn" name="decision" value="deny">deny</button></form>'
            f'<p class="foot">action {_esc(request.action_hash[:12])} · '
            f"policy {_esc(request.policy_version)} · run {_esc(request.run_id[:12])}"
            f" · rev {request.run_version}{_esc(left)}</p></div>"
        )

    def _bot_row(self, entry: RosterEntry, now: int) -> str:
        """One bot: what it is doing, and when it wakes."""
        delay = entry.due_in(now)
        when = "—" if delay is None else ("due" if delay == 0 else _short(delay))
        disc = _DISC.get(entry.status, "idle")
        why = entry.bot.paused_reason or _reason(entry, now)
        return (
            '<div class="card"><div class="row">'
            f'<span class="dot {disc}"></span>'
            f'<span><a class="name" href="/bots/{_esc(entry.bot.slug)}?token={_q(self.token)}">'
            f"{_esc(entry.bot.slug)}</a> "
            f'<span class="sm mut">{_esc(entry.status.value)}</span>'
            f'<span class="why">{_esc(why)}</span></span>'
            f'<span class="when">{when}</span></div></div>'
        )

    # ── one bot ───────────────────────────────────────────────────

    def detail(self, slug: str, *, now: int) -> str | None:
        """
        Everything about one bot: what it is, what it did, what it said.

        The three questions an operator has about a bot they are worried about,
        in the order they ask them — what is it *for*, what has it *done*, and
        what is it *waiting on*.

        :param slug: The bot's addressable name.
        :param now: Epoch seconds.
        :returns: An HTML document, or ``None`` when there is no such bot.
        """
        bot = self.bots.by_slug(slug)
        if bot is None:
            return None
        entry = next((row for row in roster(self.bots, now=now) if row.bot.id == bot.id), None)
        status = entry.status.value if entry else "unknown"
        delay = entry.due_in(now) if entry else None

        body = [
            f'<div class="top"><a class="sm mut" href="/bots?token={_q(self.token)}">'
            "&larr; fleet</a>"
            f'<span class="sm mut">{_esc(status)}'
            f"{'' if delay is None else f' · due in {_short(delay)}'}</span></div>",
            f"<h1>{_esc(bot.display_name)}</h1>",
            f'<p class="mut">{_esc(bot.title or "")}</p>',
        ]

        if bot.paused_reason:
            body.append(f'<div class="card err">Paused: {_esc(bot.paused_reason)}</div>')

        body.append('<p class="grp">What it is for</p>')
        body.append(
            f'<div class="card">{_esc(bot.mission)}<p class="why">{_esc(bot.persona)}</p></div>'
        )

        body.append('<p class="grp">How it is scheduled</p>')
        wake = bot.wake.to_dict()
        body.append(
            '<div class="card"><table>'
            + "".join(
                f'<tr><td class="sm mut">{_esc(key)}</td>'
                f'<td class="mono sm">{_esc(value)}</td></tr>'
                for key, value in sorted(wake.items())
            )
            + f'<tr><td class="sm mut">idle streak</td>'
            f'<td class="mono sm">{bot.idle_streak}</td></tr>'
            f'<tr><td class="sm mut">error streak</td>'
            f'<td class="mono sm">{bot.error_streak}</td></tr>'
            "</table></div>"
        )

        body.append('<p class="grp">Its iterations</p>')
        runs = self.bots.runs_for(bot.id, limit=12)
        if not runs:
            body.append('<div class="empty">It has not run yet.</div>')
        else:
            body.append(
                '<div class="card"><table>'
                + "".join(
                    f'<tr><td class="mono sm">{_esc(str(run["id"])[:12])}</td>'
                    f'<td class="sm">{_esc(run["state"])}</td>'
                    f'<td class="mono sm">{_esc(run["outcome"] or "—")}</td>'
                    f'<td class="sm mut">{_esc(run["terminal_reason"] or "")}</td></tr>'
                    for run in runs
                )
                + "</table></div>"
            )

        body.append('<p class="grp">Its channel</p>')
        messages = self.messages.channel(bot.id, limit=40)
        if not messages:
            body.append('<div class="empty">Nothing said yet.</div>')
        for message in messages:
            body.append(
                '<div class="card"><div class="row">'
                f'<span class="dot {"act" if message.kind.value == "ask" else "idle"}"></span>'
                f'<span><span class="sm mut">{_esc(message.kind.value)} · '
                f"{_esc(message.author)}</span>"
                f'<span class="why">{_esc(message.body)}</span></span>'
                f'<span class="when">#{message.seq}</span></div></div>'
            )

        return _page(bot.slug, "".join(body))

    def proposals(self, *, now: int) -> str:
        """
        Bots that other bots have asked for, and what they would actually get.

        The rationale is what the bot says it wants. The definition is what it
        would get, and approving the first without reading the second is how a
        plausible sentence becomes a workload pointed somewhere it should not
        be — so both are here, and the definition is not collapsed.

        :param now: Epoch seconds.
        :returns: An HTML document.
        """
        pending = self.spawns.pending() if self.spawns is not None else []
        body = [
            f'<div class="top"><a class="sm mut" href="/bots?token={_q(self.token)}">'
            "&larr; fleet</a>"
            f'<span class="sm mut">{len(pending)} proposed</span></div>',
            "<h1>Proposed bots</h1>",
        ]
        if not pending:
            body.append('<div class="empty">No bot has asked for another one.</div>')
        for request in pending:
            parent = self.bots.get(request.parent_bot_id)
            definition = "\n".join(
                f"{key}: {value}" for key, value in sorted(request.definition.items())
            )
            body.append(
                '<div class="card"><div class="row"><span class="dot act"></span>'
                f'<span><span class="name">{_esc(request.slug)}</span> '
                f'<span class="sm mut">proposed by '
                f"{_esc(parent.slug if parent else request.parent_bot_id[:12])} · "
                f"{request.allowance} iterations carved from it</span>"
                f'<span class="why">{_esc(request.rationale)}</span></span>'
                f'<span class="when">{_short(now - request.created_at)} ago</span></div>'
                f'<pre class="defn">{_esc(definition)}</pre>'
                f'<p class="foot">army bots adopt {_esc(request.id[:12])} --yes'
                "  |  army bots refuse "
                f"{_esc(request.id[:12])}</p></div>"
            )
        return _page("Proposed bots", "".join(body))

    # ── actions ───────────────────────────────────────────────────

    def verdict(self, form: dict[str, list[str]], *, now: int) -> tuple[str, str]:
        """
        Answer a question from the page.

        The same bound path the CLI uses — the page is a renderer, not a second
        authorisation route. A verdict that no longer matches its question is
        refused here exactly as it would be there.

        :param form: The submitted fields.
        :param now: Epoch seconds.
        :returns: ``(notice, problem)``, one of which is empty.
        """
        approval_id = _first(form, "approval")
        choice = _first(form, "choice")
        if not _ID.match(approval_id):
            # Exact ids only. A prefix search lets a caller send `00%` and have
            # the store pick which question they meant.
            return "", "that is not an approval id"
        request = self.approvals.get(approval_id)
        if request is None:
            return "", "no approval matching that id"

        # An explicit decision, and nothing else counts. `choice != "__deny__"`
        # meant a POST with no fields at all was an approval — and since a bot
        # authors its own option labels, the primary button could read "Deny
        # this request" and approve. Neither a missing nor an unrecognised
        # decision is treated as one.
        decision = _first(form, "decision")
        if decision not in ("approve", "deny"):
            return "", "a verdict must say approve or deny"
        approved = decision == "approve"
        run = self.store.get_run(request.run_id)
        try:
            command = self.approvals.decide(
                request,
                approved=approved,
                # Not "human". The page authenticates a *token*, which says the
                # caller could read a file bots cannot — not who they are. Writing
                # "human" into the audit trail would assert something nothing proved.
                decided_by="web:token",
                now=now,
                choice=choice if approved else None,
                # Never None: a missing run must fail the check, not skip it.
                run_version=run.version if run is not None else -1,
            )
        except (ApprovalRefused, ConcurrentTransition) as exc:
            return "", str(exc)

        self.messages.post(
            request.bot_id,
            "web:token",
            MessageKind.VERDICT,
            f"{'Approved' if approved else 'Denied'}" + (f": {choice}" if approved else ""),
            now=now,
            payload={"approval_id": request.id, "approved": approved},
            thread_id=request.thread_id,
            run_id=request.run_id,
            command_id=command.id,
        )
        return f"Recorded {command.kind.value}; the loop applies it on its next tick.", ""

    def sign(self, form: dict[str, list[str]], *, now: int) -> tuple[str, str]:
        """
        Answer an owner-only verb, on the owner's own path.

        ``spend``, ``execute_order`` and ``add_dependency`` are refused
        everywhere else — a click in a bot's channel does not reach this, and
        neither does anything a bot can call. What makes *this* the owner path
        is the same property the token has: the signing key is in an
        environment a bot's sandbox does not get, asserted by
        ``tests/army/test_gates.py``'s leak check.

        The grant is minted against the operation digest the row already holds,
        so it authorises this operation and no other; it is spent through
        ``used_grants`` on first use, so the same approval cannot pay twice.

        :param form: ``approval``, ``decision``, and ``confirm`` — which must
            be present and affirmative for an approval. A signature nobody
            actively gave is the failure mode this whole path exists to avoid.
        :param now: Epoch seconds.
        :returns: ``(notice, problem)``, one of which is empty.
        """
        approval_id = _first(form, "approval")
        if not _ID.match(approval_id):
            return "", "that is not an approval id"
        request = self.approvals.get(approval_id)
        if request is None:
            return "", "no approval matching that id"
        if not request.requires_owner:
            # Not merely unnecessary — routing an ordinary verb through the
            # owner path would spend a grant on a question that never needed
            # one, and log it as an owner decision.
            return "", f"{request.verb} is not owner-only; answer it in the channel"

        decision = _first(form, "decision")
        if decision not in ("sign", "refuse"):
            return "", "a verdict must say sign or refuse"
        approved = decision == "sign"
        if approved and _first(form, "confirm") != "yes":
            return "", "the owner confirmation was not given"

        broker = self.approvals.broker
        if approved and broker is None:
            return "", (
                f"{request.verb} is owner-only and no broker is configured. "
                "Set ARMY_BROKER_KEY on the control plane and try again."
            )

        run = self.store.get_run(request.run_id)
        try:
            grant = (
                broker.sign(
                    request.verb,
                    {"action_hash": request.action_hash},
                    now=now,
                    owner_confirmed=True,
                )
                if approved and broker is not None
                else None
            )
            command = self.approvals.decide(
                request,
                approved=approved,
                decided_by="web:owner",
                now=now,
                choice=_first(form, "choice") or None if approved else None,
                run_version=run.version if run is not None else -1,
                grant=grant,
            )
        except (ApprovalRefused, ConcurrentTransition, GateRefused) as exc:
            return "", str(exc)

        self.messages.post(
            request.bot_id,
            "web:owner",
            MessageKind.VERDICT,
            ("Signed and released" if approved else "Refused") + f": {request.verb}",
            now=now,
            payload={"approval_id": request.id, "approved": approved, "owner": True},
            thread_id=request.thread_id,
            run_id=request.run_id,
            command_id=command.id,
        )
        return f"Recorded {command.kind.value}; the loop applies it on its next tick.", ""

    def wheel_for_profile(self, profile: str, *, now: int) -> str:
        """
        Whether a browser action on this profile must be refused, and why.

        Keyed by browser profile because that is the thing being contended: one
        page, and either a person or a bot is driving it. The first version
        joined session to bot by scanning recent runs for a matching session
        id, which quietly stopped working the moment a run finished and dropped
        its outstanding sessions — the wheel read as free while somebody was
        holding it.

        Fails **open** — an unknown profile, a missing wheel store, or a bot
        nobody is driving all answer "go ahead". A control-plane hiccup that
        silently froze every bot's browser would be a much worse failure than
        one missed refusal, and the refusal is a courtesy to the agent rather
        than the boundary: nothing here is what stops a bot doing something it
        must not.

        :param profile: The browser profile the action would drive.
        :param now: Epoch seconds.
        :returns: The refusal to hand back, or ``""`` to proceed.
        """
        if self.wheels is None or not profile:
            return ""
        held = self.wheels.all_held(now=now)
        if not held:
            return ""
        wanted = _bare_profile(profile)
        for bot_id in held:
            bot = self.bots.get(bot_id)
            if bot is not None and _bare_profile(bot.browser_profile or "") == wanted:
                return REFUSAL
        return ""

    def wheel(self, form: dict[str, list[str]], *, now: int) -> tuple[str, str]:
        """
        Take a bot's browser, or hand it back.

        OpenBot's rule, kept verbatim because the alternative is worse: while a
        person is driving, the bot's browser actions are **refused rather than
        queued**. A queued click lands after the human has navigated away, on a
        page that is no longer the one it was reasoned about.

        Both directions are announced in the channel. The bot's own record of
        why it was interrupted is the thing that makes a refusal readable
        afterwards rather than a mysterious gap in an iteration.

        :param form: ``bot`` and ``action`` (``take`` or ``release``).
        :param now: Epoch seconds.
        :returns: ``(notice, problem)``, one of which is empty.
        """
        if self.wheels is None:
            return "", "this control plane has no browser wheel"
        bot = self.bots.by_slug(_first(form, "bot"))
        if bot is None:
            return "", "no bot by that name"
        action = _first(form, "action")
        if action not in ("take", "release"):
            return "", "say take or release"

        if action == "release":
            self.wheels.release(bot.id)
            self.messages.post(
                bot.id, "human:channel", MessageKind.EVENT, "Wheel handed back.", now=now
            )
            return f"{bot.slug} has its browser back.", ""

        held = self.wheels.take(
            bot.id, "human:channel", now=now, reason=_first(form, "why") or None
        )
        self.messages.post(
            bot.id,
            "human:channel",
            MessageKind.EVENT,
            "A person took the wheel. Browser actions are refused until they hand it back.",
            now=now,
            payload={"held_until": held.held_until},
        )
        return "You have the wheel. Its browser actions are refused until you release it.", ""

    def say(self, form: dict[str, list[str]], *, now: int) -> tuple[str, str]:
        """
        Say something to a bot.

        HITL was approve-or-deny and nothing else, which makes a bot a vending
        machine: you may accept what it offers or refuse it, and you may not
        ask it a question, correct a wrong assumption, or change its mind
        halfway. A channel that only a bot may write to is not a channel.

        This records the message and pulls the bot's wake forward. It does
        **not** reach into a running session from here — the same reason a
        verdict does not: this process holds no vendor client, and the loop is
        the only thing that should touch a body. The message is owed to the
        bot, so the next tick either forwards it into the live session or puts
        it in the brief for the next iteration. Either way it survives a
        restart, which a direct call would not.

        A message is not an answer. An open approval stays open — saying "why
        did you rule that out?" must never read as approval, and a channel
        where discussion silently authorises is worse than one with no
        discussion at all.

        :param form: ``bot`` and ``text``.
        :param now: Epoch seconds.
        :returns: ``(notice, problem)``, one of which is empty.
        """
        bot = self.bots.by_slug(_first(form, "bot"))
        if bot is None:
            return "", "no bot by that name"
        text = _first(form, "text").strip()
        if not text:
            return "", "nothing to say"
        if len(text) > MAX_MESSAGE_CHARS:
            return "", f"{len(text)} characters; the channel takes {MAX_MESSAGE_CHARS}"

        self.messages.post(
            bot.id,
            # Not "human" — this page authenticates a token, which proves the
            # caller could read a file bots cannot, not who they are.
            "human:channel",
            MessageKind.HUMAN_MSG,
            text,
            now=now,
            # `authorises: False` is load-bearing and read by the supervisor:
            # it is what stops a chat message being mistaken for a verdict.
            payload={"authorises": False},
            deliver_to=[bot.address],
        )

        live = self.bots.live_run_ids().get(bot.id)
        if live is not None:
            return "Sent. The loop hands it to the running iteration on its next tick.", ""
        if bot.status is not BotStatus.ACTIVE:
            return (
                f"Recorded. {bot.slug} is {bot.status.value}; it will read this when it runs.",
                "",
            )
        with suppress(ConcurrentTransition):
            # Imported here rather than at module scope: `supervisor` pulls in
            # the whole loop, and this page is also served by processes that
            # never run one.
            from army.bots.supervisor import wake_now

            wake_now(self.bots, bot, now=now, reason="human")
        return "Sent. It wakes now and reads this first.", ""

    def adopt(self, form: dict[str, list[str]], *, now: int) -> tuple[str, str]:
        """
        Decide a bot another bot asked for.

        Three outcomes, not two, because the store draws the line in two
        places. ``SpawnStore.activate`` creates the child in ``DRAFT``: saying
        the bot *should exist* is a different act from *switching it on*, so
        approving ten proposals in a row has not started ten bots. This
        exposes both, and switching on is the only one that also writes a wake.

        The caps that matter — depth, fan-out, fleet size, and the allowance
        carved from the parent rather than added to the pool — are enforced in
        the store, so this only decides.

        :param form: ``spawn``, ``decision`` (``activate``, ``draft`` or
            ``refuse``), and an optional ``because``.
        :param now: Epoch seconds.
        :returns: ``(notice, problem)``, one of which is empty.
        """
        if self.spawns is None:
            return "", "proposals are not enabled on this control plane"
        spawn_id = _first(form, "spawn")
        if not _ID.match(spawn_id):
            return "", "that is not a proposal id"
        request = self.spawns.get(spawn_id)
        if request is None:
            return "", "no proposal matching that id"

        decision = _first(form, "decision")
        if decision not in ("activate", "draft", "refuse"):
            return "", "a decision must say activate, draft or refuse"
        try:
            if decision == "refuse":
                self.spawns.refuse(
                    request,
                    decided_by="web:token",
                    because=_first(form, "because") or "refused from the page",
                    now=now,
                )
                return f"{request.slug} was refused; nothing was created.", ""

            bot = self.spawns.activate(request, decided_by="web:token", now=now)
            if decision == "draft":
                return (
                    f"{bot.slug} exists as a draft with {request.allowance} iterations. "
                    "Switch it on when you are ready.",
                    "",
                )
            # A first wake, or it would be active and never due — which derives
            # as `blocked` and reads as a bug rather than as a bot nobody woke.
            self.bots.set_status(
                bot, BotStatus.ACTIVE, now=now, wake=first_wake(bot.wake, now=now)
            )
        except (SpawnRefused, BudgetExhausted, ConcurrentTransition) as exc:
            # BudgetExhausted is the common one and it is not an error: the
            # parent cannot afford the child it asked for. Letting it escape
            # would 500 the page on the most ordinary refusal there is.
            return "", str(exc)
        return f"{bot.slug} is active, with {request.allowance} iterations.", ""

    def _lineage_json(self, bot: Any) -> dict[str, Any]:
        """
        Who this bot came from and what came from it.

        The chain is already in the data — ``parent_bot_id``, ``root_bot_id``
        and ``depth`` have been columns since the first release — and nothing
        rendered it, so the caps that bound replication were invisible at
        exactly the moment they matter: when you are deciding whether to adopt
        one more bot.

        Budget is the one that actually bites, so it is reported rather than
        described: a child's allowance is carved out of its parent's remaining,
        not added to the pool.
        """
        parent = self.bots.get(bot.parent_bot_id) if bot.parent_bot_id else None
        siblings = self.bots.children(parent.id) if parent else []
        children = self.bots.children(bot.id)

        def brief(entry: Any) -> dict[str, Any]:
            account = self.budgets.account(entry.id) if self.budgets is not None else None
            return {
                "slug": entry.slug,
                "status": entry.status.value,
                "depth": entry.depth,
                "remaining": account.remaining if account else None,
            }

        return {
            "depth": bot.depth,
            "max_depth": MAX_DEPTH,
            "max_fanout": MAX_FANOUT,
            "parent": brief(parent) if parent else None,
            # Excluding itself: "1 of 3 children under scout" is the number the
            # fan-out cap is about, and counting yourself in it reads as one
            # more sibling than exists.
            "siblings": [brief(entry) for entry in siblings if entry.id != bot.id],
            "children": [brief(entry) for entry in children],
            # Retiring a parent retires everything below it. Worth saying on
            # the page, because it is the one action here with a blast radius
            # larger than the row you clicked.
            "cascades": bool(children),
        }

    def files_json(self, slug: str, *, path: str, read: bool) -> str | None:
        """
        List a directory in a bot's workspace, or read one file out of it.

        The workspace is real — ``charter.md``, ``runbook.md``, ``lessons.md``
        and ``reports/`` are files on disk that a person edits to steer the bot
        — so the page browses it rather than drawing a picture of it. The first
        cut of the dock listed those three names as a hardcoded array, which
        looked like a file browser and was a drawing of one.

        :param slug: Which bot.
        :param path: A path *relative to the workspace root*. Anything that
            escapes the root is refused rather than clamped — a traversal is a
            bug or an attack, and silently serving the wrong directory hides
            both.
        :param read: Return the file's text instead of a listing.
        :returns: A JSON document, or ``None`` when there is no such bot.
        """
        bot = self.bots.by_slug(slug)
        if bot is None:
            return None
        root = self.workspace.path_for(bot)
        if not root.exists():
            return json.dumps(
                {
                    "root": str(root),
                    "reason": ("no workspace on disk yet; a bot gets one on its first run"),
                }
            )
        try:
            target = (root / path).resolve()
            root_real = root.resolve()
            # `relative_to` raises unless target is genuinely inside the root,
            # which is the check — string prefixes match `/ws/../ws-evil`.
            target.relative_to(root_real)
        except (ValueError, OSError):
            return json.dumps({"root": str(root), "error": "that path is outside the workspace"})

        if not target.exists():
            return json.dumps({"root": str(root), "error": "no such file"})

        if read:
            if target.is_dir():
                return json.dumps({"root": str(root), "error": "that is a directory"})
            size = target.stat().st_size
            if size > MAX_READ_BYTES:
                return json.dumps(
                    {
                        "root": str(root),
                        "path": path,
                        "too_big": True,
                        "bytes": size,
                        "error": f"{size} bytes; the viewer stops at {MAX_READ_BYTES}",
                    }
                )
            try:
                text = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return json.dumps({"root": str(root), "path": path, "binary": True, "bytes": size})
            return json.dumps({"root": str(root), "path": path, "bytes": size, "text": text})

        entries = []
        for child in sorted(
            target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
        ):
            if child.name.startswith("."):
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": str(child.relative_to(root_real)),
                    "dir": child.is_dir(),
                    "bytes": None if child.is_dir() else stat.st_size,
                    "modified_at": int(stat.st_mtime),
                }
            )
        return json.dumps({"root": str(root), "path": path, "entries": entries})

    def bot_json(self, slug: str, *, now: int) -> str | None:
        """
        One bot as JSON: definition, ledger, channel, and its open question.

        Everything the middle column and the dock of the Bots section need, in
        one round trip — a page that fetched them separately would show a
        roster and a channel from two different moments.

        :param slug: The bot's addressable name.
        :param now: Epoch seconds.
        :returns: A JSON document, or ``None`` when there is no such bot.
        """
        bot = self.bots.by_slug(slug)
        if bot is None:
            return None
        entry = next((row for row in roster(self.bots, now=now) if row.bot.id == bot.id), None)
        pending = self.approvals.pending(bot_id=bot.id)
        return json.dumps(
            {
                "slug": bot.slug,
                "display_name": bot.display_name,
                "title": bot.title,
                "mission": bot.mission,
                "persona": bot.persona,
                "workload": bot.workload,
                "harness": bot.harness,
                "wake": bot.wake.to_dict(),
                "status": entry.status.value if entry else "unknown",
                "due_in": entry.due_in(now) if entry else None,
                "idle_streak": bot.idle_streak,
                "error_streak": bot.error_streak,
                "paused_reason": bot.paused_reason,
                "workspace": bot.workspace,
                "browser_profile": bot.browser_profile,
                "lineage": self._lineage_json(bot),
                "revision": bot.current_revision_id,
                "runs": [
                    {
                        "id": run["id"],
                        "state": run["state"],
                        "outcome": run["outcome"],
                        "reason": run["terminal_reason"],
                        "at": run["updated_at"],
                        # The Omnigent conversation the iteration ran in. This
                        # is what makes the harness reachable: a bot's body is
                        # an ordinary session, and the app already has a very
                        # good page for one.
                        "session_id": run["session_id"],
                    }
                    for run in self.bots.runs_for(bot.id, limit=12)
                ],
                "channel": [
                    {
                        "seq": message.seq,
                        "kind": message.kind.value,
                        "author": message.author,
                        "body": message.body,
                        "at": message.created_at,
                        "thread": message.thread_id,
                    }
                    for message in self.messages.channel(bot.id, limit=60)
                ],
                "pending": [
                    {
                        "id": request.id,
                        "question": request.question,
                        "options": request.options,
                        "evidence": request.evidence,
                        "verb": request.verb,
                        "action_hash": request.action_hash,
                        "policy_version": request.policy_version,
                        "run_id": request.run_id,
                        "run_version": request.run_version,
                        "requires_owner": request.requires_owner,
                        "expires_at": request.expires_at,
                        "created_at": request.created_at,
                    }
                    for request in pending
                ],
            },
            indent=2,
        )

    def api(self, *, now: int) -> str:
        """
        The roster as JSON, for anything that is not a browser.

        :param now: Epoch seconds.
        :returns: A JSON document.
        """
        entries = roster(self.bots, now=now)
        return json.dumps(
            {
                "counts": summarise(entries),
                "bots": [
                    {
                        "slug": entry.bot.slug,
                        "status": entry.status.value,
                        "next_due_at": entry.bot.next_due_at,
                        "due_in": entry.due_in(now),
                        "idle_streak": entry.bot.idle_streak,
                        "error_streak": entry.bot.error_streak,
                        "last_outcome": (
                            entry.bot.last_outcome.value if entry.bot.last_outcome else None
                        ),
                        "paused_reason": entry.bot.paused_reason,
                        "run_id": entry.live_run_id,
                        "title": entry.bot.title,
                        "mission": entry.bot.mission,
                        "harness": entry.bot.harness,
                        "needs_human": entry.needs_a_human,
                        "blocked_until": entry.blocked_until,
                        "wake_kind": entry.bot.wake.kind.value,
                    }
                    for entry in entries
                ],
                "pending": [
                    {
                        "id": request.id,
                        "bot_id": request.bot_id,
                        "question": request.question,
                        "options": request.options,
                        "requires_owner": request.requires_owner,
                        "expires_at": request.expires_at,
                    }
                    for request in self.approvals.pending()
                ],
                # Owner-only verbs, separated because they are answered on a
                # different path and must never be rendered as an ordinary
                # question with an approve button.
                "owner": [
                    self._owner_json(request)
                    for request in self.approvals.pending()
                    if request.requires_owner
                ],
                # Whether a signature is possible at all. Without a key the
                # page must say so rather than offer a button that is certain
                # to be refused.
                "can_sign": self.approvals.broker is not None,
                # Who is driving each browser. The page renders it and the
                # desktop relay consults it before claiming an action, so one
                # answer serves the display and the enforcement.
                "driving": {
                    self.bots.get(bot_id).slug: {  # type: ignore[union-attr]
                        "driver": held.driver,
                        "since": held.taken_at,
                        "until": held.held_until,
                        "reason": held.reason,
                    }
                    for bot_id, held in (
                        self.wheels.all_held(now=now) if self.wheels is not None else {}
                    ).items()
                    if self.bots.get(bot_id) is not None
                },
                "drafts": [self._draft_json(request) for request in self._proposals()],
            },
            indent=2,
        )

    def _proposals(self) -> list[Any]:
        """Pending spawn requests, or nothing when proposals are disabled."""
        return list(self.spawns.pending()) if self.spawns is not None else []

    def _owner_json(self, request: Any) -> dict[str, Any]:
        """
        One owner-only request, with everything a signature would be bound to.

        The digest is what the grant is signed over, so it is shown. Signing
        something whose fingerprint you were never given is signing a blank.
        """
        bot = self.bots.get(request.bot_id)
        return {
            "id": request.id,
            "bot_id": request.bot_id,
            "bot": bot.slug if bot else request.bot_id[:12],
            "verb": request.verb,
            "question": request.question,
            "options": request.options,
            "evidence": request.evidence,
            "action_hash": request.action_hash,
            "digest": digest(request.verb, {"action_hash": request.action_hash}),
            "policy_version": request.policy_version,
            "run_id": request.run_id,
            "run_version": request.run_version,
            "expires_at": request.expires_at,
            "created_at": request.created_at,
        }

    def _draft_json(self, request: Any) -> dict[str, Any]:
        """
        One proposal: the pitch, and the definition it would actually create.

        Both, never just the first. The rationale is what the bot says it
        wants; the definition is what it would get, and approving the one
        without reading the other is the whole attack.
        """
        parent = self.bots.get(request.parent_bot_id)
        return {
            "id": request.id,
            "slug": request.slug,
            "parent": parent.slug if parent else request.parent_bot_id[:12],
            "rationale": request.rationale,
            "allowance": request.allowance,
            "definition": request.definition,
            "created_at": request.created_at,
        }


class _Handler(BaseHTTPRequestHandler):
    """Routes three paths and nothing else.

    :param site: The renderer.
    """

    server_version = "army-bots"

    def __init__(self, site: BotsSite, *args: Any, **kwargs: Any) -> None:
        self.site = site
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        route = urlparse(self.path)
        params = parse_qs(route.query)
        now = int(time.time())
        if not self.site.authorises(self._token(params)):
            self._refuse()
            return
        if route.path == "/api/bots":
            self._send(200, self.site.api(now=now), "application/json")
        elif route.path.startswith("/api/wheel/"):
            # The browser profile, URL-quoted: it contains a colon.
            profile = unquote(route.path.removeprefix("/api/wheel/"))
            refusal = self.site.wheel_for_profile(profile, now=now)
            self._send(200, json.dumps({"refuse": refusal}), "application/json")
        elif route.path.startswith("/api/files/"):
            payload = self.site.files_json(
                route.path.removeprefix("/api/files/"),
                path=_first(params, "path"),
                read=_first(params, "read") == "1",
            )
            if payload is None:
                self._send(404, '{"error":"no such bot"}', "application/json")
            else:
                self._send(200, payload, "application/json")
        elif route.path.startswith("/api/bots/"):
            payload = self.site.bot_json(route.path.removeprefix("/api/bots/"), now=now)
            if payload is None:
                self._send(404, '{"error":"no such bot"}', "application/json")
            else:
                self._send(200, payload, "application/json")
        elif route.path == "/proposals":
            self._send(200, self.site.proposals(now=now), "text/html; charset=utf-8")
        elif route.path.startswith("/bots/"):
            page = self.site.detail(route.path.removeprefix("/bots/"), now=now)
            if page is None:
                self._send(404, _page("Not found", "<h1>No such bot</h1>"), _HTML)
            else:
                self._send(200, page, _HTML)
        elif route.path in ("/", "/bots"):
            self._send(
                200,
                self.site.index(
                    now=now,
                    notice=_NOTICES.get(_first(params, "ok"), ""),
                    problem=_NOTICES.get(_first(params, "err"), _first(params, "err")),
                ),
                _HTML,
            )
        else:
            self._send(404, _page("Not found", "<h1>Not found</h1>"), _HTML)

    def _token(self, params: dict[str, list[str]]) -> str:
        """
        The secret this request carried, from a header or the query string.

        The header is the right place; the query string exists so a link can be
        opened on a phone, which is the whole point of the page.

        :param params: Parsed query string.
        :returns: The token, or an empty string.
        """
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return _first(params, "token")

    def _refuse(self) -> None:
        """Say no without saying what would have been there."""
        self._send(
            401,
            _page(
                "Unauthorised",
                '<h1>Unauthorised</h1><p class="mut">This page needs the token from '
                '<span class="mono">~/.omnigent/army/web-token</span>. '
                '<span class="mono">army bots serve</span> prints the link.</p>',
            ),
            _HTML,
        )

    def do_POST(self) -> None:
        route = urlparse(self.path)
        # Three write paths, and they are separate on purpose: an ordinary
        # verdict, an owner signature, and adopting a proposed bot are three
        # different authorities, so one handler cannot be talked into doing
        # the wrong one of them with an unexpected field.
        actions = {
            "/verdict": self.site.verdict,
            "/owner/sign": self.site.sign,
            "/spawn/adopt": self.site.adopt,
            "/say": self.site.say,
            "/wheel": self.site.wheel,
        }
        action = actions.get(route.path)
        if action is None:
            self._send(404, "", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        # Bounded: an unbounded read from a socket is a way to be stopped by
        # anyone who can reach the port.
        if length > 64_000:
            self._send(413, "", "text/plain")
            return
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        form.update(parse_qs(route.query))
        if not self.site.authorises(self._token(form)):
            self._refuse()
            return
        if not self._same_origin():
            # Cross-site request forgery. There are no cookies to be SameSite
            # about, but a form POST is a simple request needing no preflight,
            # so any page the operator opens could otherwise answer a named id.
            self._send(403, _page("Refused", "<h1>Refused</h1>"), _HTML)
            return

        notice, problem = action(form, now=int(time.time()))
        # Redirect after post, so a refresh does not re-answer the question,
        # and carry a code rather than prose — a message echoed into the page
        # is a phishing surface inside a card the operator trusts.
        code = "recorded" if notice else "refused"
        detail = "" if notice else f"&err={_q(problem)}"
        self.send_response(303)
        self.send_header("Location", f"/bots?token={_q(self.site.token)}&ok={code}{detail}")
        self.end_headers()

    def _same_origin(self) -> bool:
        """
        Whether the request came from this page rather than from another site.

        ``Sec-Fetch-Site`` first, because it is the header that actually
        answers the question and every current browser sends it. ``Origin`` is
        the fallback for a client that does not — and an ``Origin`` of the
        literal string ``null`` is not a mismatch: browsers send it for an
        ordinary form POST from a plain-HTTP page, so treating it as hostile
        rejects the page's own buttons.

        This is defence in depth rather than the control. The token is not a
        cookie, so a cross-site attacker cannot get the browser to attach it —
        they would have to know it already, and then they need no victim.

        :returns: Whether the request may be acted on.
        """
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site:
            return site in ("same-origin", "same-site", "none")
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return True
        return urlparse(origin).netloc == (self.headers.get("Host") or "")

    def _send(self, status: int, body: str, content_type: str) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # This page renders bot output. Nothing it shows may execute.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # The token is in the URL so a phone can open a link. Nothing may cache
        # a page that lists pending approvals, still less one carrying a secret.
        self.send_header("Cache-Control", "no-store, private")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Route access logs through logging rather than stderr."""
        _logger.debug("%s - %s", self.address_string(), fmt % args)


def serve(
    site: BotsSite,
    *,
    host: str = "127.0.0.1",
    port: int = 6768,
) -> ThreadingHTTPServer:
    """
    Start the page.

    Bound to loopback by default. The deployment is reached over Tailscale, so
    the tailnet address is the thing to bind when it should be reachable from a
    phone — and binding ``0.0.0.0`` on a laptop in a café is not that.

    Reaching the port is **not** authority. Bots run on this box with network
    access, so "only the tailnet can reach it" would have meant every bot could
    read the pending approvals and answer its own — a complete bypass of the
    approval machinery, by the thing the machinery exists to gate, with the
    verdict recorded as a human's. Every request carries the token instead.

    :param site: The renderer.
    :param host: Address to bind.
    :param port: Port to bind.
    :returns: The running server.
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        _logger.warning(
            "serving the bots page on %s:%d — bind a tailnet address, never a public "
            "one. The token is the authorisation; the network is not.",
            host,
            port,
        )
    return ThreadingHTTPServer((host, port), partial(_Handler, site))


#: The one content type this page serves.
_HTML = "text/html; charset=utf-8"

#: Confirmations the page may show, keyed by a code the redirect carries.
#: Echoing arbitrary prose back into a card the operator trusts is a phishing
#: surface — "Approved by the owner, verified" renders identically to real text.
_NOTICES: dict[str, str] = {
    "recorded": "Recorded. The loop applies it on its next tick.",
    "refused": "That verdict was refused.",
}


def _page(title: str, body: str) -> str:
    """Wrap a fragment in the document shell."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)} · Omnigent</title><style>{_STYLE}</style></head>"
        f'<body><div class="wrap">{body}</div></body></html>'
    )


def _reason(entry: RosterEntry, now: int) -> str:
    """The line under a bot's name, when there is something worth saying."""
    if entry.status is DerivedStatus.WAITING_RESOURCE and entry.blocked_until:
        return f"{entry.bot.harness} lane cooling for {_short(entry.blocked_until - now)}"
    if entry.status is DerivedStatus.BACKING_OFF:
        return f"nothing to do {entry.bot.idle_streak}× running"
    if entry.status is DerivedStatus.BLOCKED:
        return "no next wake and nothing pending — it needs waking by hand"
    if entry.status is DerivedStatus.RUNNING and entry.live_run_id:
        return f"run {entry.live_run_id[:12]}"
    if entry.bot.error_streak:
        return f"failed {entry.bot.error_streak}× running"
    return entry.bot.mission


def _counts(counts: dict[str, int]) -> str:
    """The header line: what the fleet is doing, in one sentence."""
    return " · ".join(f"{count} {status}" for status, count in sorted(counts.items())) or "no bots"


def _first(form: dict[str, list[str]], key: str) -> str:
    """The first value for a field, or an empty string."""
    values = form.get(key) or []
    return values[0] if values else ""


def _short(seconds: int) -> str:
    """Render a delay in the least noisy unit that is still honest."""
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _esc(text: object) -> str:
    """Escape anything that came from a bot before it reaches the page."""
    return html.escape(str(text), quote=True)


def _q(text: str) -> str:
    """Escape a value for a query string."""
    from urllib.parse import quote

    return quote(text[:300])
