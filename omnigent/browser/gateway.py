"""A server-owned browser that a headless agent can drive, and a person can watch.

The embedded browser in the desktop app is a real Chromium page driven through
``browser_*`` and displayed by an Electron overlay. That is the right shape and
it has one requirement a background fleet cannot meet: **a subscribed
renderer**. A bot running under ``army bots run`` on a box with no desktop app
open has nowhere for its browser actions to land, and they time out.

This is the other executor for the same tools. The action arrives on the same
route, carries the same arguments, and returns the same result shape — only the
thing performing it changes, from "whichever Electron window claimed it" to a
Chromium this process owns.

## Why that matters more than it sounds

The page shown to a person and the page the agent drives are **the same page**,
by construction rather than by convention. A screenshot taken beside an agent's
own fetching would be a picture of a browser nobody was using; this is the
browser being used. It is also what makes handing over the wheel meaningful:
there is one page, so taking it is taking *that*.

## What it does not see

A vendor CLI can still fetch a URL with its own tooling, and nothing here
observes that. This is the bot's browser, not a network monitor, and the UI
says so rather than implying otherwise. The same lesson the workspace root
taught: state the boundary you actually have.

## Cost

One Chromium is 200–400MB. A profile is launched on first use and closed after
:data:`IDLE_CLOSE_S` of nothing, and the number resident at once is capped —
nine bots are nine identities, not nine resident browsers. Frames are captured
on demand and after each action rather than streamed, because a bot acts in
discrete steps and a frame per action is both cheaper and more truthful than
thirty a second of a page sitting still.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
import secrets
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

#: Where per-profile browser data lives. One directory per profile, so a
#: profile's cookies are its own — sharing one across bots means one bot's
#: compromise is every bot's session, and an audit trail that cannot attribute.
PROFILE_ROOT = Path.home() / ".omnigent" / "browser-profiles"

#: Chromium instances resident at once. Small, because a browser each for ten
#: bots is three gigabytes of mostly idle memory — but deliberately *above* the
#: supervisor's default of three concurrent runs. At two, three bots browsing
#: meant every launch closed somebody's page, and the steady state of a nine-bot
#: fleet was ping-pong rather than two stable identities.
MAX_RESIDENT = 4

#: How long a profile with nothing to do stays open. The profile survives on
#: disk; only the process goes.
IDLE_CLOSE_S = 600.0

#: How long one action may take before the caller gets a clean error. Below the
#: route's own await budget, so the gateway answers rather than being cut off.
ACTION_TIMEOUT_S = 25.0

#: Frames are JPEG at this quality. Legible text, small enough to hand to a
#: browser over a local connection several times a second.
FRAME_QUALITY = 60

#: How many past actions the trail keeps. Enough to see how a page was reached,
#: short enough that it stays readable next to the frame.
TRAIL_LENGTH = 30

#: How much page text one snapshot returns.
SNAPSHOT_CHARS = 20_000

#: How many interactive elements one snapshot names. A ref list is what lets an
#: agent click something it just read about; unbounded, a link farm would spend
#: the whole context describing itself.
MAX_REFS = 200

#: The attribute a snapshot stamps on interactive elements so a later click can
#: name one. Written into the live DOM, which is why it is namespaced.
REF_ATTR = "data-omni-ref"

#: Tags and roles worth naming. Everything here is something a person could
#: click or type into; static text gets read from the snapshot instead.
_INTERACTIVE = (
    "a[href],button,input,select,textarea,summary,"
    '[role="button"],[role="link"],[role="tab"],[role="checkbox"],'
    '[contenteditable="true"],[onclick]'
)

_SNAPSHOT_JS = r"""
(args) => {
  const [selector, refAttr, maxRefs, chars] = args;
  const elements = [];
  let n = 0;
  for (const el of document.querySelectorAll(selector)) {
    if (elements.length >= maxRefs) break;
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height) continue;
    const ref = ++n;
    el.setAttribute(refAttr, String(ref));
    const type = el.getAttribute('type') || '';
    // Never el.value. Deciding "is this secret?" here would decide it in the
    // page's own JS world, where `Array.prototype.includes`, `getAttribute`
    // and `type` are all things the page can rewrite — so one line of hostile
    // script turns a password box into a text box and the plaintext a human
    // typed while holding the wheel lands in the transcript. A value is never
    // needed to *name* a control, so it is never read.
    const label = (
      el.getAttribute('aria-label') ||
      el.innerText ||
      el.getAttribute('placeholder') ||
      el.getAttribute('title') ||
      ''
    );
    elements.push({
      ref,
      tag: el.tagName.toLowerCase(),
      type: type || undefined,
      name: String(label).replace(/\s+/g, ' ').trim().slice(0, 80),
    });
  }
  return {text: (document.body?.innerText ?? '').slice(0, chars), elements};
}
"""


#: Field names that mean "credential" whatever the input's ``type`` says. A
#: one-time code is almost always ``type="text"`` with ``inputmode="numeric"``,
#: because that is what makes phone keyboards behave — so filtering on
#: ``type="password"`` alone misses every 2FA prompt ever shipped, which is
#: exactly the field an injected agent would be steered towards.
_SECRET_NAME = re.compile(
    r"(?:^|[^a-z])("
    r"password|passwd|passcode|"
    r"otp|totp|mfa|2fa|one-?time-?code|verification-?code|auth-?code|"
    r"current-password|new-password|"
    r"cc-?num|card-?number|cvv|cvc|csc|"
    r"pin|secret|token|api-?key|private-?key|seed-?phrase|mnemonic"
    r")(?:[^a-z]|$)"
)


#: The only schemes a bot's browser may reach. ``file:`` is the one that
#: matters: this process's own profile root holds every other bot's cookie
#: database, and ``file:///…/browser-profiles/bot-b/Default/Cookies`` is a
#: complete cross-bot credential theft through a tool that looks like reading a
#: page. ``chrome:``, ``devtools:`` and ``blob:`` are excluded for the same
#: reason — they address the browser rather than the web.
#: ``data:`` is here and ``file:`` is not, and the difference is the point: a
#: data URL is inert markup in an opaque origin with no cookies and no disk,
#: while a file URL reads this machine. Rendering attacker-chosen HTML is
#: something any navigation can do anyway.
ALLOWED_SCHEMES = frozenset({"http", "https", "data"})

#: Hostnames that resolve to this machine or its network neighbours. A bot's
#: browser runs on the server, so ``http://127.0.0.1:6769`` is the *control
#: plane* — the thing holding the fleet's token and every pending approval —
#: and ``169.254.169.254`` is the cloud metadata service. Neither is a page.
_BLOCKED_NETWORKS = (
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "fc00::/7",
    "fe80::/10",
    "0.0.0.0/8",
)


def _blocked_reason(url: str) -> str:
    """
    Why a bot's browser must not go here, or ``""``.

    Checked on the resolved address rather than the name, because
    ``evil.example`` is free to have an A record of ``127.0.0.1``. This is not
    a complete SSRF defence — DNS can answer differently on the second lookup —
    and it is applied to every request rather than only the first, which is
    where a redirect would otherwise walk straight through.

    :param url: Where the browser is being sent.
    :returns: A refusal, or ``""``.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return f"{parts.scheme or 'that'}: URLs are not reachable from a bot's browser"
    if scheme == "data":
        return ""
    host = parts.hostname or ""
    if not host:
        return "that URL names no host"
    if host.lower().endswith(".local") or host.lower() == "localhost":
        return f"{host} is this machine, which is not a page"
    for candidate in _resolved(host):
        for network in _BLOCKED_NETWORKS:
            if candidate in ipaddress.ip_network(network):
                return (
                    f"{host} resolves to {candidate}, which is this machine or its "
                    "private network — a bot's browser reaches the public web only"
                )
    return ""


def _resolved(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """
    Every address a hostname answers to, plus the literal if it is one.

    A name that will not resolve returns nothing, which reads as "not
    obviously private" — the request will fail on its own merits.

    :param host: The hostname or literal address.
    :returns: The addresses.
    """
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return found


#: What a bot is told when a person has the wheel. Written for an agent rather
#: than a log: it says what happened, that waiting is correct, and that
#: retrying is not. Mirrors ``army.bots.wheel.REFUSAL`` — the two are the same
#: sentence on either side of a boundary neither may import across.
WHEEL_REFUSAL = (
    "A person has taken the wheel of this browser. Your action was refused, "
    "not queued — the page may be somewhere else by the time they hand it "
    "back. Wait, say in your reply that you were interrupted, and do not "
    "retry in a loop."
)


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed, or no browser could be started."""


@dataclass
class _Profile:
    """One bot's browser: a context, its page, and when it was last useful."""

    name: str
    context: Any
    page: Any
    last_used: float
    #: The most recent frame, as a data URL. Kept so a page that opens the
    #: viewer sees the current state without waiting for the next action.
    frame: str | None = None
    #: Serialises actions on this page. Two clicks interleaved on one page is
    #: a class of bug nobody can reproduce.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: How many screenshots this profile has taken, so each gets its own file
    #: and an agent comparing before-and-after still has the before.
    shots: int = 0
    #: How many snapshots, and the id of the newest. ``None`` means the refs in
    #: the DOM belong to a document that is gone.
    snapshot: int = 0
    snapshot_id: str | None = None
    #: What each ref described when the snapshot named it, so an action can
    #: check it is still acting on that. ``{ref: {"tag", "name"}}``.
    refs: dict[int, dict[str, str]] = field(default_factory=dict)
    #: When a person's hold on this browser lapses, on the monotonic clock.
    #: ``0.0`` means the bot is driving. A time rather than a flag so a control
    #: plane that dies mid-hold does not brick the browser forever.
    driving_until: float = 0.0
    #: The last :data:`TRAIL_LENGTH` actions, so a person can see what the bot
    #: did rather than only where it ended up. Bounded because this lives for
    #: as long as the browser does and nothing prunes it.
    trail: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=TRAIL_LENGTH))


class BrowserGateway:
    """Owns the browsers, one per profile, and performs actions on them.

    :param root: Where profile directories live.
    :param max_resident: Chromium instances open at once.
    """

    def __init__(self, root: Path | None = None, *, max_resident: int = MAX_RESIDENT) -> None:
        self.root = root or PROFILE_ROOT
        #: At least one, or ``_evict_if_full`` loops on an empty dict forever
        #: and ``min()`` raises on the empty sequence.
        self.max_resident = max(1, max_resident)
        self._profiles: dict[str, _Profile] = {}
        #: Holds by profile, kept outside :attr:`_profiles` so taking the wheel
        #: of a browser that is not open yet still refuses the bot when it
        #: opens one.
        self._held: dict[str, float] = {}
        self._playwright: Any = None
        self._lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────

    async def _ensure_playwright(self) -> Any:
        """
        Start Playwright once, lazily.

        Imported here rather than at module scope on purpose: Playwright is a
        *test* dependency of this project, and importing it at startup would
        promote it to a runtime one for every deployment, including the ones
        that never open a browser. A deployment that wants bot browsing installs
        it; the rest get a clean refusal.
        """
        if self._playwright is not None:
            return self._playwright
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise BrowserUnavailable(
                "the browser gateway needs Playwright: `uv pip install playwright` "
                "and `playwright install chromium`"
            ) from exc
        self._playwright = await async_playwright().start()
        return self._playwright

    async def _profile_for(self, profile: str) -> _Profile:
        """
        The browser for one profile, launching it if it is not resident.

        The context owns the profile directory for its whole life, which is
        what keeps Chromium's ``SingletonLock`` honest: one process holds it,
        and a second run connects to this gateway rather than launching its own.
        Deleting that lockfile to "fix" a stuck profile corrupts it; closing the
        owner is the fix.
        """
        # One spelling wins. ``persist:bot-x`` from a bot definition and
        # ``bot-x`` from the Screen panel are the same browser; keyed apart they
        # would be two Chromiums fighting over one profile's SingletonLock.
        name = _canonical(profile)
        if not name:
            raise BrowserUnavailable(
                f"{profile!r} is not a usable browser profile name — "
                "letters, digits, dot, dash and underscore only"
            )
        existing = self._profiles.get(name)
        if existing is not None:
            existing.last_used = time.monotonic()
            return existing

        async with self._lock:
            # Re-check: another caller may have launched it while we waited.
            existing = self._profiles.get(name)
            if existing is not None:
                return existing
            await self._evict_if_full()
            playwright = await self._ensure_playwright()
            directory = self.root / name
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # Wrapped, because everything above returns a tool result and
                # an OSError here escaped `perform` entirely — the agent got an
                # HTTP 500 instead of the clean error the docstring promises.
                raise BrowserUnavailable(f"could not make room for {name}: {exc}") from exc
            try:
                context = await playwright.chromium.launch_persistent_context(
                    str(directory),
                    headless=True,
                    viewport={"width": 1280, "height": 800},
                )
            except Exception as exc:
                raise BrowserUnavailable(f"could not start a browser for {name}: {exc}") from exc
            # Every request, not only the one the agent asked for. A redirect
            # to file:// or to the loopback control plane is the same theft
            # with one extra hop, and a subresource never passes through
            # `navigate` at all.
            await context.route("**/*", _guard_request)
            page = context.pages[0] if context.pages else await context.new_page()
            entry = _Profile(name=name, context=context, page=page, last_used=time.monotonic())

            # Any navigation, not just the ones the agent asked for. Clearing
            # refs only in the `navigate` action left them live through a
            # click-through, a 302, a meta refresh and an SPA route change —
            # every way a page actually moves — with the stamps sitting on
            # detached or recycled nodes and the snapshot id still current.
            def _forget_refs(frame: Any, _entry: _Profile = entry) -> None:
                if frame is _entry.page.main_frame:
                    _entry.snapshot_id = None
                    _entry.refs = {}

            page.on("framenavigated", _forget_refs)
            self._profiles[name] = entry
            _logger.info("browser gateway: opened profile %s", name)
            return entry

    async def _evict_if_full(self) -> None:
        """
        Make room for another browser, without taking one that is in use.

        Eviction used to pick the least recently used and close it — with no
        regard for ``entry.lock``. So a third bot waking up called
        ``context.close()`` underneath another bot's in-flight click: that run
        got ``Target closed`` with no hint its browser had been taken, its next
        action silently relaunched a blank page, and the page state, the open
        tab and the trail were gone. ``last_used`` is stamped *after* a
        successful action, so a bot in the middle of a slow one looked idle and
        was preferentially killed.

        :raises BrowserUnavailable: When every resident browser is busy.
        """
        while len(self._profiles) >= self.max_resident:
            idle = [entry for entry in self._profiles.values() if not entry.lock.locked()]
            if not idle:
                raise BrowserUnavailable(
                    f"all {self.max_resident} browsers are busy; try again shortly"
                )
            await self.close(min(idle, key=lambda entry: entry.last_used).name)

    async def close(self, name: str) -> None:
        """
        Close one profile's browser. Its directory survives.

        :param name: The profile.
        """
        entry = self._profiles.pop(_canonical(name), None)
        if entry is None:
            return
        try:
            await entry.context.close()
        except Exception:  # noqa: BLE001 - closing a dead browser is not an error
            _logger.debug("browser gateway: %s was already gone", name, exc_info=True)
        _logger.info("browser gateway: closed profile %s", name)

    async def sweep(self) -> None:
        """
        Close profiles nothing has used for a while.

        Called from :meth:`perform` and :meth:`frame` rather than from a timer:
        it had no caller at all, which made :data:`IDLE_CLOSE_S` documentation
        rather than behaviour and left a logged-in browser resident for as long
        as the server ran. An uncalled reaper reads exactly like a working one.

        Never closes a browser somebody is using, for the same reason eviction
        does not.
        """
        cutoff = time.monotonic() - IDLE_CLOSE_S
        stale = [
            name
            for name, entry in self._profiles.items()
            if entry.last_used < cutoff and not entry.lock.locked()
        ]
        for name in stale:
            _logger.info("browser gateway: %s idle for %.0fs; closing", name, IDLE_CLOSE_S)
            await self.close(name)

    async def shutdown(self) -> None:
        """Close everything, for a server that is stopping."""
        for name in list(self._profiles):
            await self.close(name)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    # ── the actions ───────────────────────────────────────────────

    def hold(self, profile: str, *, seconds: float) -> None:
        """
        Record that a person is driving this browser.

        Held **here**, next to the browser, rather than asked over HTTP before
        each action. The first version asked the control plane on every action
        and then ran for up to 25 seconds, so a person could take the wheel a
        millisecond after the check cleared and the bot would still snapshot
        the login form they were typing into. It also failed open on every
        error — a rotated token, a 2s timeout, a renamed function — which meant
        the one moment the wheel exists for was the moment it stopped working.

        :param profile: Whose browser.
        :param seconds: How long the hold lasts.
        """
        entry = self._profiles.get(_canonical(profile))
        if entry is not None:
            entry.driving_until = time.monotonic() + seconds
        self._held[_canonical(profile)] = time.monotonic() + seconds

    def release(self, profile: str) -> None:
        """
        Hand a browser back to its bot.

        :param profile: Whose browser.
        """
        name = _canonical(profile)
        entry = self._profiles.get(name)
        if entry is not None:
            entry.driving_until = 0.0
        self._held.pop(name, None)

    def driven_by_a_person(self, profile: str) -> bool:
        """
        Whether a person holds this browser right now.

        :param profile: Whose browser.
        :returns: Whether a bot's actions must be refused.
        """
        return self._held.get(_canonical(profile), 0.0) > time.monotonic()

    async def perform(self, profile: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """
        Do one thing to a page and say what happened.

        Returns the same ``{ok, ...}`` shape the desktop relay returns, because
        the caller is the same route and the agent must not be able to tell
        which executor ran — one tool, two transports.

        :param profile: Whose browser.
        :param action: The ``browser_`` tool name minus the prefix.
        :param args: The tool's arguments.
        :returns: The action result.
        """
        await self.sweep()
        try:
            entry = await self._profile_for(profile)
        except BrowserUnavailable as exc:
            return {"ok": False, "error": str(exc)}

        async with entry.lock:
            # Inside the lock, so an action cannot slip past a hold taken while
            # it queued. It cannot abort one already running — that would need
            # a cancel the page has no notion of — but nothing new starts.
            if self.driven_by_a_person(profile):
                result = {"ok": False, "error": WHEEL_REFUSAL}
                self._record(entry, action, args, result)
                return result
            try:
                result = await asyncio.wait_for(
                    self._dispatch(entry, action, args), timeout=ACTION_TIMEOUT_S
                )
            except TimeoutError:
                # Deliberately not a retry. A click that timed out may have
                # landed; repeating it is how one order becomes two.
                result = {
                    "ok": False,
                    "error": f"{action} did not finish in {ACTION_TIMEOUT_S:.0f}s",
                }
                self._record(entry, action, args, result)
                # Capture on the way out, not only on success. The frame a
                # person watches went stale exactly when something interesting
                # was going wrong — a hung click leaves the last *good* picture
                # on screen, which is the most misleading thing it could show.
                await self._capture(entry)
                return result
            except Exception as exc:  # noqa: BLE001 - a page can fail in any way
                result = {"ok": False, "error": _safe_error(f"{action} failed: {exc}")}
                self._record(entry, action, args, result)
                await self._capture(entry)
                return result
            self._record(entry, action, args, result)
            entry.last_used = time.monotonic()
            await self._capture(entry)
            return result

    async def _drifted(self, entry: _Profile, args: dict[str, Any], selector: str) -> str:
        """
        Whether the element behind a ref is still the one the snapshot named.

        The ref is an attribute **in the page's own DOM**, which the page can
        read and move. A hostile page that sees ``data-omni-ref="35"`` on a
        harmless button can relocate it onto "Confirm transfer", and the
        agent's next click — correct by every other check, with a current
        snapshot id — lands there while the transcript still says it was the
        button it read about.

        Checked through Playwright's locator API rather than ``page.evaluate``,
        deliberately: ``evaluate`` runs in the page's own JavaScript world,
        where ``getAttribute`` and ``innerText`` are things the page can
        rewrite. A verification a hostile page can implement is not one.

        :param entry: The profile.
        :param args: The tool's arguments.
        :param selector: The resolved selector.
        :returns: A refusal, or ``""`` to go ahead.
        """
        ref = args.get("ref")
        if ref is None or not str(ref).strip().isdigit():
            return ""
        expected = entry.refs.get(int(str(ref).strip()))
        if expected is None:
            return ""
        page = entry.page
        try:
            if await page.locator(f"{expected['tag']}{selector}").count() != 1:
                return (
                    f"ref {ref} named a {expected['tag']} and no longer does — the page "
                    "moved it. Take a new browser_snapshot and read it before acting."
                )
            actual = await _describe(page.locator(selector).first)
        except Exception:  # noqa: BLE001 - a page that will not answer is drift enough
            return f"could not check what ref {ref} points at; take a new browser_snapshot"
        if actual != expected["name"]:
            return (
                f"ref {ref} named {expected['name']!r} and now points at {actual!r} — "
                "the page moved it. Take a new browser_snapshot and read it before acting."
            )
        return ""

    def _record(
        self, entry: _Profile, action: str, args: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """
        Keep a short, readable trail of what this browser was made to do.

        A live frame answers "where is it now" and nothing else. Watching a bot
        work means seeing the steps — and after the fact, a refused or wrong
        action is only explicable if what was attempted was written down. This
        is that record: what, to what, and whether it worked.

        Never the typed text, and never a screenshot's bytes. The trail is
        shown in a UI and read by whoever asks; a password that reaches it is
        a password in a log.

        :param entry: The profile.
        :param action: The action performed.
        :param args: Its arguments.
        :param result: What came back.
        """
        entry.trail.append(
            {
                "action": action,
                "target": _target_summary(action, args),
                "ok": bool(result.get("ok")),
                "url": _safe_url(str(result.get("url") or "")),
                "error": _safe_error(str(result.get("error") or "")),
            }
        )

    def trail(self, profile: str) -> list[dict[str, Any]]:
        """
        What this browser was recently made to do, oldest first.

        :param profile: Whose browser.
        :returns: The recorded actions, or ``[]`` for a browser never opened.
        """
        entry = self._profiles.get(_canonical(profile))
        return list(entry.trail) if entry is not None else []

    async def _dispatch(
        self, entry: _Profile, action: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Route one action to the page."""
        page = entry.page
        if action == "navigate":
            url = str(args.get("url", ""))
            if not url:
                return {"ok": False, "error": "navigate needs a url"}
            blocked = _blocked_reason(url)
            if blocked:
                return {"ok": False, "error": blocked}
            await page.goto(url, wait_until="domcontentloaded")
            # Every ref belonged to the old document. Dropping the snapshot id
            # turns a click on a stale ref into a clear "snapshot first" rather
            # than an eight-second wait for a selector that cannot match.
            entry.snapshot_id = None
            entry.refs = {}
            return {"ok": True, "url": page.url, "title": await page.title()}
        if action == "snapshot":
            # Text plus a ref for everything clickable. The text alone is what
            # this returned first, and an agent that can read a page but cannot
            # name anything on it has to guess CSS selectors or give up — the
            # first live run gave up and fetched the HTML with curl instead.
            result = await page.evaluate(
                _SNAPSHOT_JS,
                [_INTERACTIVE, REF_ATTR, MAX_REFS, SNAPSHOT_CHARS],
            )
            elements = result.get("elements") or []
            entry.snapshot += 1
            # A nonce, not a counter. A relaunched profile starts counting at
            # one again, so `snap_1` from before an eviction would match
            # `snap_1` after it — the staleness check passing on a different
            # document in a different browser.
            entry.snapshot_id = f"snap_{secrets.token_hex(8)}"
            entry.refs = {
                int(element["ref"]): {
                    "tag": str(element.get("tag") or ""),
                    "name": str(element.get("name") or ""),
                }
                for element in elements
            }
            return {
                "ok": True,
                "snapshot_id": entry.snapshot_id,
                "url": page.url,
                "title": await page.title(),
                "text": result.get("text", ""),
                "tree": "\n".join(_line(element) for element in elements),
                # What this snapshot could not see, said out loud. An agent
                # that believes it has seen every control concludes the one it
                # wants does not exist — and a consent, OAuth or payment form
                # is *always* in an iframe, which this cannot read at all.
                "truncated": len(elements) >= MAX_REFS,
                "hidden_frames": max(0, len(page.frames) - 1),
                "note": (
                    "this snapshot covers the main document only; "
                    f"{len(page.frames) - 1} embedded frame(s) were not read"
                    if len(page.frames) > 1
                    else ""
                ),
            }
        if action in ("click", "type"):
            selector, error = _target(args, entry)
            if error:
                return {"ok": False, "error": error}
            drifted = await self._drifted(entry, args, selector)
            if drifted:
                return {"ok": False, "error": drifted}
            if action == "type" and await _is_secret(page, selector):
                # The one place the "ask a person to sign in" rule stops being
                # an instruction and becomes a mechanism. A brief can be argued
                # with by the page it is reading; this cannot.
                return {
                    "ok": False,
                    "error": (
                        "that is a password field. This browser is shared with a "
                        "person: say what is being asked for and at what URL, and "
                        "stop. Do not look for a credential."
                    ),
                }
            if action == "click":
                await page.click(selector, timeout=8_000)
                return {"ok": True, "url": page.url}
            await page.fill(selector, str(args.get("text", "")), timeout=8_000)
            # Deliberately no echo of the text. A tool result is transcript.
            return {"ok": True, "url": page.url}
        if action == "screenshot":
            # A path, not the pixels. Returning a data URL put 60k characters
            # of base64 into one tool result, which overran the model's output
            # limit and cost the agent two turns working around it. A file the
            # agent can read when it actually needs to look is both smaller and
            # what it wanted; the live frame for a watching human is served by
            # :meth:`frame`, which never goes near the model.
            shot = await page.screenshot(type="jpeg", quality=FRAME_QUALITY)
            path = self._frame_path(entry)
            path.write_bytes(shot)
            return {
                "ok": True,
                "path": str(path),
                "bytes": len(shot),
                "url": page.url,
                "hint": "read this path to look at the page",
            }
        return {"ok": False, "error": f"unknown browser action {action!r}"}

    def _frame_path(self, entry: _Profile) -> Path:
        """
        Where this profile's next screenshot goes.

        Under the profile's own directory. They used to share one ``_shots``
        folder with the profile name in the filename, which handed every bot a
        readable picture of every other bot's logged-in pages — the exact
        cross-contamination :data:`PROFILE_ROOT` exists to prevent, undone by
        the one artifact that is a photograph of the session.
        """
        directory = self.root / entry.name / "shots"
        directory.mkdir(parents=True, exist_ok=True)
        entry.shots += 1
        return directory / f"{entry.shots:03d}.jpg"

    # ── what a person sees ────────────────────────────────────────

    async def _capture(self, entry: _Profile) -> None:
        """Keep the latest frame, so opening the viewer shows the page now."""
        try:
            shot = await entry.page.screenshot(type="jpeg", quality=FRAME_QUALITY)
        except Exception:  # noqa: BLE001 - a closing page is not worth an error
            return
        entry.frame = _data_url(shot)

    async def frame(self, profile: str, *, fresh: bool = False) -> dict[str, Any]:
        """
        The current view of a profile's page.

        :param profile: Whose browser.
        :param fresh: Take a new screenshot rather than returning the frame
            kept after the last action. What a person watching wants; the
            cached one is for opening the panel without waking a browser.
        :returns: ``{ok, dataUrl, url, title}`` or a refusal.
        """
        entry = self._profiles.get(_canonical(profile))
        if entry is None:
            # Never launches. Looking at a bot is not asking it to browse, and
            # ``fresh`` used to mean "start one if there isn't one" — so opening
            # the panel on an idle bot spent 300MB on a blank page and, at a
            # resident cap of two, could evict the browser a working bot was
            # in the middle of using.
            return {"ok": False, "error": "that browser is not open"}
        if fresh:
            async with entry.lock:
                await self._capture(entry)
        try:
            title = await entry.page.title()
            url = _safe_url(entry.page.url)
        except Exception:  # noqa: BLE001
            title, url = "", ""
        if entry.frame is None:
            # Open, but nothing has been drawn yet. Reporting ok with a null
            # image left the panel showing an empty box where a page should be.
            return {"ok": False, "error": "that browser has not loaded a page yet"}
        return {"ok": True, "dataUrl": entry.frame, "url": url, "title": title}

    def resident(self) -> list[str]:
        """Which profiles have a browser open right now."""
        return sorted(self._profiles)


async def _describe(locator: Any) -> str:
    """
    The name a control goes by, read the way the snapshot named it.

    Through the locator API, so the answer comes from Playwright's isolated
    world rather than from functions the page is free to redefine.

    :param locator: The element.
    :returns: Its name, trimmed the same way the snapshot trims.
    """
    for attribute in ("aria-label",):
        value = await locator.get_attribute(attribute)
        if value:
            return " ".join(str(value).split())[:80]
    text = await locator.inner_text()
    if text:
        return " ".join(str(text).split())[:80]
    for attribute in ("placeholder", "title"):
        value = await locator.get_attribute(attribute)
        if value:
            return " ".join(str(value).split())[:80]
    return ""


async def _is_secret(page: Any, selector: str) -> bool:
    """
    Whether a field is one no bot may type into.

    Read off the live element through the locator API — isolated world, so a
    page cannot answer this question on its own behalf — and off the live
    element rather than the snapshot, because a page can swap a text input for
    a password one between the two.

    Fails **closed**. A page that will not say what a field is is not a page to
    type into.

    :param page: The page.
    :param selector: The resolved selector.
    :returns: Whether typing must be refused.
    """
    try:
        locator = page.locator(selector).first
        if (await locator.get_attribute("type") or "").strip().lower() == "password":
            return True
        described = ""
        for attribute in ("name", "autocomplete", "id", "aria-label", "placeholder"):
            described += " " + (await locator.get_attribute(attribute) or "")
        described = described.lower()
    except Exception:  # noqa: BLE001
        return True
    return bool(_SECRET_NAME.search(described))


def _safe_url(url: str) -> str:
    """
    A URL with the parts that carry secrets removed.

    Scheme, host and path say where the browser is, which is what a person
    watching needs. The query, the fragment and any ``user:pass@`` say *who it
    is* — an OAuth callback is ``?code=…&state=…`` and an implicit-flow return
    is ``#access_token=…``, both live credentials, both landing in a trail that
    is rendered in a UI and screenshotted into tickets. A magic sign-in link is
    the whole session in a query string.

    :param url: The URL.
    :returns: Scheme, host and path, with a marker when anything was dropped.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unreadable url)"
    if not parts.scheme:
        return url[:120]
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    trimmed = f"{parts.scheme}://{host}{port}{parts.path}"[:200]
    if parts.query or parts.fragment or parts.username:
        trimmed += " (query withheld)"
    return trimmed


#: Anything URL-shaped inside a longer string. Playwright quotes the URL it was
#: working on into its exception text, so a failed navigation to an OAuth
#: callback puts the authorization code in an error message — which is recorded,
#: returned to the model, and rendered in the UI.
_URL_IN_TEXT = re.compile(r"""[a-zA-Z][a-zA-Z0-9+.-]*://[^\s"'<>]+""")


def _safe_error(text: str) -> str:
    """
    An error message with every URL in it redacted, and bounded.

    :param text: The message.
    :returns: Something safe to record.
    """
    return _URL_IN_TEXT.sub(lambda match: _safe_url(match.group(0)), text)[:400]


async def _guard_request(route: Any, request: Any) -> None:
    """
    Let a request through, or abort it with a reason in the log.

    :param route: Playwright's route handle.
    :param request: The request.
    """
    blocked = _blocked_reason(request.url)
    if blocked:
        _logger.warning("browser gateway: refused %s — %s", _safe_url(request.url), blocked)
        await route.abort("blockedbyclient")
        return
    await route.continue_()


#: What a browser profile may be called. Deliberately narrow, and deliberately
#: *validated* rather than sanitised — see :func:`_canonical`.
_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def _canonical(profile: str) -> str:
    """
    One canonical name for a profile, or ``""`` when it is not a usable name.

    Two rules, both learned the hard way.

    **Case-folded**, because a profile is a directory: the Screen panel asking
    for ``Bot-A`` and the supervisor labelling sessions ``persist:bot-a`` were
    two keys in this process and *one* directory on macOS and Windows — two
    live Chromiums holding one profile's ``SingletonLock``, which is the exact
    corruption ``PROFILE_ROOT`` exists to prevent, on the platform most of this
    is developed on.

    **Refused, not repaired.** The first version replaced anything outside a
    small alphabet with ``_``, so ``acme/prod``, ``acme:prod``, ``acme prod``
    and ``acme_prod`` were one browser sharing one cookie jar — four
    identities, one session, and an audit trail that cannot say which of them
    did a thing. Silently merging identities is worse than refusing a name.

    :param profile: The profile name, with or without its ``persist:`` prefix.
    :returns: The canonical name, or ``""`` when it is unusable.
    """
    bare = profile.removeprefix("persist:").strip().casefold()
    if ".." in bare or not _PROFILE_NAME.fullmatch(bare):
        return ""
    return bare


def _target_summary(action: str, args: dict[str, Any]) -> str:
    """
    What an action was aimed at, in a few words and with no secrets in it.

    ``type`` names its field and never its text: the trail is shown in a UI,
    and a password that reaches a UI is a password in a screenshot.

    :param action: The action.
    :param args: Its arguments.
    :returns: A short description, possibly empty.
    """
    if action == "navigate":
        return _safe_url(str(args.get("url") or ""))
    if action in ("click", "type"):
        ref = args.get("ref")
        where = f"ref {ref}" if ref is not None else "(no ref)"
        return f"{where} (text withheld)" if action == "type" else where
    return ""


def _line(element: dict[str, Any]) -> str:
    """One snapshot element, in the ``[ref=N]`` form the tool schema promises."""
    kind = element.get("type") or element.get("tag")
    name = element.get("name") or ""
    return f'- {kind} "{name}" [ref={element.get("ref")}]'


def _target(args: dict[str, Any], entry: _Profile) -> tuple[str, str]:
    """
    Resolve a click/type target to a selector, or say precisely what is wrong.

    A ``ref`` only means anything against the snapshot that minted it. The
    attribute is re-stamped on every snapshot, so ref 3 is a different element
    after the next one — acting on a superseded ref would click a plausible
    wrong thing silently, which is the worst available outcome.

    :param args: The tool's arguments.
    :param entry: The profile, holding the current snapshot id.
    :returns: ``(selector, "")`` or ``("", reason)``.
    """
    ref = args.get("ref")
    if ref is not None and str(ref).strip() != "":
        if entry.snapshot_id is None:
            return "", "no snapshot to resolve that ref against — take a browser_snapshot first"
        given = str(args.get("snapshot_id") or "").strip()
        if not given:
            # Required, not encouraged. Optional, the check was opt-out by the
            # one party it exists to constrain: an injected agent simply omits
            # the field and every stale ref is live again.
            return "", (
                f"pass the snapshot_id that ref {ref} came from — a ref without it "
                "cannot be checked against the page it was read from"
            )
        if given != entry.snapshot_id:
            return "", (
                f"ref {ref} came from {given}, which has been superseded by "
                f"{entry.snapshot_id} — take a browser_snapshot and use its refs"
            )
        # Digits only: this is concatenated into a selector, and a ref is
        # generated, so anything else is either a bug or an injection attempt.
        if not str(ref).strip().isdigit():
            return "", f"ref must be a non-negative integer, got {ref!r}"
        return f'[{REF_ATTR}="{str(ref).strip()}"]', ""
    if str(args.get("selector") or "").strip():
        # Refused, not honoured. A raw selector is a second channel that walks
        # past every control the ref path provides: `input[type=password]`
        # picks the field the agent was told never to touch, `xpath=` and
        # Playwright's `>>` reach into frames the snapshot never showed, and
        # none of it appears in the snapshot the operator can read. Anything
        # worth acting on has a ref.
        return "", (
            "selectors are not accepted — take a browser_snapshot and act on a "
            "[ref=N] from it, so what you act on is something you have read"
        )
    return "", "needs a ref from a recent browser_snapshot"


def _data_url(image: bytes) -> str:
    """A JPEG as a data URL, which is what an ``<img>`` wants."""
    return "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")


#: The process-wide gateway. One per server, because it owns OS processes and
#: profile directories — two would fight over the same ``SingletonLock``.
_GATEWAY: BrowserGateway | None = None


def gateway() -> BrowserGateway:
    """The process's browser gateway, built on first use."""
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = BrowserGateway()
    return _GATEWAY
