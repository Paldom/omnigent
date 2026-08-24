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

    async def _profile_for(self, name: str) -> _Profile:
        """
        The browser for one profile, launching it if it is not resident.

        The context owns the profile directory for its whole life, which is
        what keeps Chromium's ``SingletonLock`` honest: one process holds it,
        and a second run connects to this gateway rather than launching its own.
        Deleting that lockfile to "fix" a stuck profile corrupts it; closing the
        owner is the fix.
        """
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
            profile = _Profile(name=name, context=context, page=page, last_used=time.monotonic())
            self._profiles[name] = profile
            _logger.info("browser gateway: opened profile %s", name)
            return profile

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
        profile = self._profiles.pop(name, None)
        if profile is None:
            return
        try:
            await profile.context.close()
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
                return {
                    "ok": False,
                    "error": f"{action} did not finish in {ACTION_TIMEOUT_S:.0f}s",
                }
            except Exception as exc:  # noqa: BLE001 - a page can fail in any way
                return {"ok": False, "error": f"{action} failed: {exc}"}
            entry.last_used = time.monotonic()
            await self._capture(entry)
            return result

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
            return {"ok": True, "url": page.url, "title": await page.title()}
        if action == "snapshot":
            # The accessibility tree, not a screenshot: it is what a model
            # reads well, and it costs a fraction of the vision tokens.
            text = await page.evaluate("() => document.body?.innerText?.slice(0, 20000) ?? ''")
            return {"ok": True, "url": page.url, "title": await page.title(), "text": text}
        if action == "click":
            selector = str(args.get("selector") or args.get("ref") or "")
            if not selector:
                return {"ok": False, "error": "click needs a selector"}
            await page.click(selector, timeout=8_000)
            return {"ok": True, "url": page.url}
        if action == "type":
            selector = str(args.get("selector") or args.get("ref") or "")
            text = str(args.get("text", ""))
            if not selector:
                return {"ok": False, "error": "type needs a selector"}
            await page.fill(selector, text, timeout=8_000)
            return {"ok": True, "url": page.url}
        if action == "screenshot":
            shot = await page.screenshot(type="jpeg", quality=FRAME_QUALITY)
            return {"ok": True, "dataUrl": _data_url(shot), "url": page.url}
        return {"ok": False, "error": f"unknown browser action {action!r}"}

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
        entry = self._profiles.get(profile)
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
