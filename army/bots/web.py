"""The Bots surface, served by this process rather than built into Omnigent.

The obvious place for a roster is a section in the Omnigent web app. It is also
the one part of Bot mode that would cost something at every rebase: upstream
hard-codes its routes and its navigation, so an integrated page means carried
patches in files that change weekly, forever, in a repository that lands about
a hundred issues a week.

Serving it here instead costs one page and no upstream files. The deployment is
already one always-on box reached over Tailscale, so a second port on that box
is the same journey for the operator — and the page can be opened from a phone,
which is the whole reason the approval path had to be answerable from one.

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
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from army.bots.approvals import ApprovalRefused, ApprovalStore
from army.bots.messages import MessageStore
from army.bots.model import DerivedStatus
from army.bots.roster import RosterEntry, roster, summarise
from army.bots.store import BotStore
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


class BotsSite:
    """Renders the fleet, and accepts a verdict.

    :param store: The run store.
    :param bots: The bot store.
    :param approvals: The approval ledger.
    :param messages: The channel, for the verdict trail.
    :param token: The shared secret every request must carry. Not a login —
        it authenticates *reaching a file bots cannot read*, which is the
        property that matters here.
    """

    def __init__(
        self,
        store: Store,
        bots: BotStore,
        approvals: ApprovalStore,
        messages: MessageStore,
        token: str,
    ) -> None:
        self.store = store
        self.bots = bots
        self.approvals = approvals
        self.messages = messages
        self.token = token

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
            f'<span><span class="name">{_esc(entry.bot.slug)}</span> '
            f'<span class="sm mut">{_esc(entry.status.value)}</span>'
            f'<span class="why">{_esc(why)}</span></span>'
            f'<span class="when">{when}</span></div></div>'
        )

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
            __import__("army.bots.messages", fromlist=["MessageKind"]).MessageKind.VERDICT,
            f"{'Approved' if approved else 'Denied'}" + (f": {choice}" if approved else ""),
            now=now,
            payload={"approval_id": request.id, "approved": approved},
            thread_id=request.thread_id,
            run_id=request.run_id,
            command_id=command.id,
        )
        return f"Recorded {command.kind.value}; the loop applies it on its next tick.", ""

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
            },
            indent=2,
        )


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
        elif route.path in ("/", "/bots"):
            self._send(
                200,
                self.site.index(
                    now=now,
                    notice=_NOTICES.get(_first(params, "ok"), ""),
                    problem=_NOTICES.get(_first(params, "err"), _first(params, "err")),
                ),
                "text/html; charset=utf-8",
            )
        else:
            self._send(404, _page("Not found", "<h1>Not found</h1>"), "text/html; charset=utf-8")

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
            "text/html; charset=utf-8",
        )

    def do_POST(self) -> None:
        route = urlparse(self.path)
        if route.path != "/verdict":
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
            self._send(403, _page("Refused", "<h1>Refused</h1>"), "text/html; charset=utf-8")
            return

        notice, problem = self.site.verdict(form, now=int(time.time()))
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

        :returns: ``True`` when the Origin is absent (a direct client, e.g.
            curl with the token) or matches the Host this was served on.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return urlparse(origin).netloc == host

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
