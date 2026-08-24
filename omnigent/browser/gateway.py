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
import logging
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

#: Chromium instances resident at once. Deliberately small: the fleet cap is
#: ten bots and a browser each would be three gigabytes of idle memory.
MAX_RESIDENT = 2

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

#: Tags a ref list must never carry a value for. A snapshot that echoes what is
#: typed into a password box puts the credential in the transcript, the run
#: artifacts, and anything downstream that reads them.
_SECRET_TYPES = ("password",)

_SNAPSHOT_JS = """
(args) => {
  const [selector, refAttr, maxRefs, chars, secretTypes] = args;
  const elements = [];
  let n = 0;
  for (const el of document.querySelectorAll(selector)) {
    if (elements.length >= maxRefs) break;
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height) continue;
    const ref = ++n;
    el.setAttribute(refAttr, String(ref));
    const type = el.getAttribute('type') || '';
    const secret = secretTypes.includes(type.toLowerCase());
    const label = (
      el.getAttribute('aria-label') ||
      (secret ? '' : el.value) ||
      el.innerText ||
      el.getAttribute('placeholder') ||
      el.getAttribute('title') ||
      ''
    );
    elements.push({
      ref,
      tag: el.tagName.toLowerCase(),
      type: type || undefined,
      name: String(label).replace(/\\s+/g, ' ').trim().slice(0, 80),
    });
  }
  return {text: (document.body?.innerText ?? '').slice(0, chars), elements};
}
"""


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
        self.max_resident = max_resident
        self._profiles: dict[str, _Profile] = {}
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
            directory.mkdir(parents=True, exist_ok=True)
            try:
                context = await playwright.chromium.launch_persistent_context(
                    str(directory),
                    headless=True,
                    viewport={"width": 1280, "height": 800},
                )
            except Exception as exc:
                raise BrowserUnavailable(f"could not start a browser for {name}: {exc}") from exc
            page = context.pages[0] if context.pages else await context.new_page()
            entry = _Profile(name=name, context=context, page=page, last_used=time.monotonic())
            self._profiles[name] = entry
            _logger.info("browser gateway: opened profile %s", name)
            return entry

    async def _evict_if_full(self) -> None:
        """Close the least recently used profile when at the cap."""
        while len(self._profiles) >= self.max_resident:
            oldest = min(self._profiles.values(), key=lambda entry: entry.last_used)
            await self.close(oldest.name)

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
        """Close profiles nothing has used for a while."""
        cutoff = time.monotonic() - IDLE_CLOSE_S
        for name in [n for n, p in self._profiles.items() if p.last_used < cutoff]:
            await self.close(name)

    async def shutdown(self) -> None:
        """Close everything, for a server that is stopping."""
        for name in list(self._profiles):
            await self.close(name)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    # ── the actions ───────────────────────────────────────────────

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
        try:
            entry = await self._profile_for(profile)
        except BrowserUnavailable as exc:
            return {"ok": False, "error": str(exc)}

        async with entry.lock:
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
                return result
            except Exception as exc:  # noqa: BLE001 - a page can fail in any way
                result = {"ok": False, "error": f"{action} failed: {exc}"}
                self._record(entry, action, args, result)
                return result
            self._record(entry, action, args, result)
            entry.last_used = time.monotonic()
            await self._capture(entry)
            return result

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
                "url": str(result.get("url") or ""),
                "error": str(result.get("error") or ""),
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
            await page.goto(url, wait_until="domcontentloaded")
            # Every ref belonged to the old document. Dropping the snapshot id
            # turns a click on a stale ref into a clear "snapshot first" rather
            # than an eight-second wait for a selector that cannot match.
            entry.snapshot_id = None
            return {"ok": True, "url": page.url, "title": await page.title()}
        if action == "snapshot":
            # Text plus a ref for everything clickable. The text alone is what
            # this returned first, and an agent that can read a page but cannot
            # name anything on it has to guess CSS selectors or give up — the
            # first live run gave up and fetched the HTML with curl instead.
            result = await page.evaluate(
                _SNAPSHOT_JS,
                [_INTERACTIVE, REF_ATTR, MAX_REFS, SNAPSHOT_CHARS, list(_SECRET_TYPES)],
            )
            elements = result.get("elements") or []
            entry.snapshot += 1
            entry.snapshot_id = f"snap_{entry.snapshot}"
            return {
                "ok": True,
                "snapshot_id": entry.snapshot_id,
                "url": page.url,
                "title": await page.title(),
                "text": result.get("text", ""),
                "tree": "\n".join(_line(element) for element in elements),
                # Say so rather than letting a truncated list read as the whole
                # page: an agent that thinks it has seen every control will
                # conclude the one it wants does not exist.
                "truncated": len(elements) >= MAX_REFS,
            }
        if action in ("click", "type"):
            selector, error = _target(args, entry)
            if error:
                return {"ok": False, "error": error}
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
        """Where this profile's next screenshot goes, outside its browser data."""
        directory = self.root / "_shots"
        directory.mkdir(parents=True, exist_ok=True)
        entry.shots += 1
        return directory / f"{entry.name}-{entry.shots:03d}.jpg"

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
            if not fresh:
                return {"ok": False, "error": "that browser is not open"}
            try:
                entry = await self._profile_for(profile)
            except BrowserUnavailable as exc:
                return {"ok": False, "error": str(exc)}
        if fresh:
            async with entry.lock:
                await self._capture(entry)
        try:
            title = await entry.page.title()
            url = entry.page.url
        except Exception:  # noqa: BLE001
            title, url = "", ""
        return {"ok": True, "dataUrl": entry.frame, "url": url, "title": title}

    def resident(self) -> list[str]:
        """Which profiles have a browser open right now."""
        return sorted(self._profiles)


def _canonical(profile: str) -> str:
    """
    A profile name reduced to one canonical, filesystem-safe form.

    Used for both the directory and the in-memory key, so a profile opened as
    ``persist:bot-x`` is the same browser the Screen panel finds under
    ``bot-x``. Two spellings of one profile would launch two Chromiums fighting
    over one ``SingletonLock``, and the panel would show the wrong one.

    Bot profiles are named ``persist:bot-<slug>`` — an Electron partition key,
    which is where that prefix comes from. The colon is dropped because this is
    a filesystem path, and everything outside a small alphabet is replaced
    rather than trusted: this string arrives from a session label, and a name
    containing ``..`` would otherwise choose the directory.

    :param profile: The profile name.
    :returns: A directory name.
    """
    bare = profile.removeprefix("persist:") or profile
    safe = "".join(char if (char.isalnum() or char in "._-") else "_" for char in bare)
    return safe.strip(".") or "default"


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
        return str(args.get("url") or "")[:120]
    ref = args.get("ref")
    selector = str(args.get("selector") or "")[:80]
    if action in ("click", "type"):
        where = f"ref {ref}" if ref is not None else selector
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
        if given and given != entry.snapshot_id:
            return "", (
                f"ref {ref} came from {given}, which has been superseded by "
                f"{entry.snapshot_id} — take a browser_snapshot and use its refs"
            )
        # Digits only: this is concatenated into a selector, and a ref is
        # generated, so anything else is either a bug or an injection attempt.
        if not str(ref).strip().isdigit():
            return "", f"ref must be a non-negative integer, got {ref!r}"
        return f'[{REF_ATTR}="{str(ref).strip()}"]', ""
    selector = str(args.get("selector") or "").strip()
    if selector:
        return selector, ""
    return "", "needs a ref from a recent browser_snapshot, or a CSS selector"


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
