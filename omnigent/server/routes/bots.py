"""Same-origin access to the Bot mode control plane.

Bot state lives in the ``army`` control plane, in its own SQLite file and its
own process, because exactly one component owns durable workflow state. The
browser cannot talk to it directly for two reasons: it is a different origin,
and the token that authorises a decision must not reach a page.

So this forwards. It imports nothing from ``army`` — the coupling is one HTTP
call to a loopback port, which is the same boundary the CLI uses — and it reads
the token from disk on the server side, where a bot's sandbox already cannot.

When Bot mode is not running, every route answers ``{"running": false}`` rather
than erroring, so the page can say "the loop is not running" instead of
rendering a failure.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Request

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

#: Where the bots page listens. Same box by construction — this is a control
#: plane for one always-on machine, not a service mesh.
DEFAULT_BASE = "http://127.0.0.1:6768"

#: Where the bots page keeps its shared secret. Inside the control-plane
#: directory, which the per-bot sandbox withholds, so reading it here is a
#: capability the server has and a bot does not.
TOKEN_FILE = Path.home() / ".omnigent" / "army" / "web-token"

#: Long enough for a local answer, short enough that a wedged control plane
#: does not wedge the page.
TIMEOUT_S = 5.0


def _base_url() -> str:
    """The bots control plane's address, overridable for a non-default port."""
    return os.environ.get("OMNIGENT_BOTS_URL", DEFAULT_BASE).rstrip("/")


def _token() -> str | None:
    """
    Read the bots page token, or ``None`` when Bot mode has never run.

    Never returned to the browser. The page asks this server, this server
    holds the secret — which is the only arrangement in which "the operator can
    decide" and "a bot cannot decide" are both true, given bots share the
    network with everything else on the box.
    """
    try:
        secret = TOKEN_FILE.read_text().strip()
    except OSError:
        return None
    return secret or None


#: How long a person's hold on a browser lasts before it falls back to the bot.
#: Mirrors ``army.bots.wheel.DEFAULT_LEASE_S``; the ledger is the record and
#: this is the enforcement, so they are set together or the browser and the
#: roster disagree about who is driving.
WHEEL_LEASE_S = 900.0


async def wheel_refusal_for_profile(profile: str) -> tuple[str, bool]:
    """
    Whether a person has taken the browser this action would drive.

    Lives here because this module already holds the loopback client and the
    token, so the browser route can ask without importing ``army`` or learning
    where the control plane is.

    Fails open on every error, deliberately: a control plane that is down must
    not freeze every bot's browser, and this refusal is a courtesy to the agent
    rather than a boundary.

    :param profile: The browser profile the action would drive.
    :returns: ``(refusal, lapsed)`` — the text to hand back, and whether the
        hold has passed its lease and is waiting on an explicit hand-back.
    """
    token = _token()
    if token is None or not profile:
        return "", False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{_base_url()}/api/wheel/{quote(profile, safe='')}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code != 200:
            return "", False
        answer = response.json()
        return str(answer.get("refuse") or ""), bool(answer.get("lapsed"))
    except (httpx.HTTPError, ValueError):
        return "", False


def create_bots_router(*, auth_provider: AuthProvider | None = None) -> APIRouter:
    """
    Build the router for the Bots section.

    :param auth_provider: Omnigent's own authentication, applied first. A
        person who may not use this server may not answer its bots either.
    :returns: The router.
    """
    router = APIRouter()

    async def _forward(
        method: str, path: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Call the bots control plane, translating "not running" into an answer.

        :param method: HTTP method.
        :param path: Path on the control plane.
        :param data: Form fields, for a POST.
        :returns: The decoded body, or a ``running: false`` sentinel.
        """
        token = _token()
        if token is None:
            return {"running": False, "reason": "Bot mode has not been started on this machine."}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                response = await client.request(
                    method,
                    f"{_base_url()}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    data=data,
                    # A verdict answers with a redirect back to the control
                    # plane's own page; the status is the result, not the body.
                    follow_redirects=False,
                )
        except httpx.HTTPError as exc:
            return {
                "running": False,
                "reason": (
                    f"The bots loop is not answering on {_base_url()} ({exc.__class__.__name__})."
                ),
            }
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            return {"running": True, "ok": "err=" not in location, "location": location}
        if response.status_code >= 400:
            return {"running": False, "reason": f"The bots loop answered {response.status_code}."}
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            return {"running": False, "reason": "The bots loop returned a non-JSON body."}
        payload["running"] = True
        return payload

    @router.get("/bots")
    async def list_bots(request: Request) -> dict[str, Any]:
        """The fleet and everything waiting on a person."""
        require_user(request, auth_provider)
        return await _forward("GET", "/api/bots")

    @router.get("/bots/{slug}")
    async def one_bot(request: Request, slug: str) -> dict[str, Any]:
        """One bot's definition, ledger, channel and open question."""
        require_user(request, auth_provider)
        return await _forward("GET", f"/api/bots/{slug}")

    @router.get("/bots/{slug}/files")
    async def files(
        request: Request, slug: str, path: str = "", read: bool = False
    ) -> dict[str, Any]:
        """
        Browse a bot's workspace, or read one file out of it.

        The bot's charter, runbook, lessons and reports are real files a person
        edits to steer it, so the panel browses them. The control plane holds
        the workspace root and refuses anything that escapes it — this only
        forwards, and deliberately does not resolve paths itself, so there is
        one place where that check lives.
        """
        require_user(request, auth_provider)
        query = urlencode({"path": path, "read": "1" if read else "0"})
        return await _forward("GET", f"/api/files/{slug}?{query}")

    @router.post("/bots/verdict")
    async def answer(request: Request) -> dict[str, Any]:
        """
        Answer a question, through the control plane's own bound path.

        Deliberately a forward rather than a reimplementation. The bindings —
        action hash, policy version, run version — are checked once, in the
        place that owns them, so this page cannot become a softer route to a
        verdict than the CLI.
        """
        require_user(request, auth_provider)
        body = await request.json()
        return await _forward(
            "POST",
            "/verdict",
            data={
                "approval": str(body.get("approval", "")),
                "choice": str(body.get("choice", "")),
                "decision": "approve" if body.get("approved") else "deny",
            },
        )

    @router.get("/bots/{slug}/screen")
    async def screen(request: Request, slug: str, fresh: bool = True) -> dict[str, Any]:
        """
        The current view of a bot's browser.

        A JPEG data URL, taken from the same Chromium page the bot's
        ``browser_*`` actions drive — so this is the browser being used rather
        than a picture of one nobody is looking at.

        Behind this server's authentication on purpose. A frame of a
        logged-in page plus an input endpoint is full session control, so it
        never leaves the authenticated origin and is never written to disk:
        a one-time code lives in a frame for as long as the frame does.
        """
        require_user(request, auth_provider)
        from omnigent.browser import gateway

        # The frame says where the browser is; the trail says how it got there.
        # A picture alone cannot distinguish a bot that read a page from one
        # that clicked through four and ended up somewhere it should not be.
        frame = await gateway().frame(f"bot-{slug}", fresh=fresh)
        frame["trail"] = gateway().trail(f"bot-{slug}")
        return frame

    @router.post("/bots/wheel")
    async def wheel(request: Request) -> dict[str, Any]:
        """
        Take a bot's browser, or hand it back.

        While a person holds it the bot's browser actions are refused rather
        than queued: a queued click lands after the human has navigated away,
        on a page that is no longer the one it was reasoned about.
        """
        require_user(request, auth_provider)
        body = await request.json()
        slug = str(body.get("bot", ""))
        take = bool(body.get("take"))
        answer = await _forward(
            "POST",
            "/wheel",
            data={
                "bot": slug,
                "action": "take" if take else "release",
                "why": str(body.get("why", "")),
            },
        )
        # Tell the browser itself, not only the ledger. The gateway is what
        # actually refuses a bot's action, and it must know before the next one
        # arrives — asking the control plane per action was a round trip that
        # failed open and left a window between the answer and the click.
        if answer.get("ok"):
            from omnigent.browser import gateway

            if take:
                gateway().hold(f"bot-{slug}", seconds=WHEEL_LEASE_S)
            else:
                gateway().release(f"bot-{slug}")
        return answer

    @router.post("/bots/react")
    async def react(request: Request) -> dict[str, Any]:
        """
        Mark a message as seen, useful, unclear or a concern.

        A separate route from the verdict for the same reason ``say`` is: an
        approval binds to an action hash, a policy version and a run version,
        and nothing that does not may stand in for it. There is deliberately no
        mark that reads as a tick — beside a pending question, a tick is a
        verdict to every human who has ever used chat software.
        """
        require_user(request, auth_provider)
        body = await request.json()
        return await _forward(
            "POST",
            "/react",
            data={
                "message": str(body.get("message", "")),
                "mark": str(body.get("mark", "")),
            },
        )

    @router.post("/bots/say")
    async def say(request: Request) -> dict[str, Any]:
        """
        Say something to a bot.

        A separate route from the verdict on purpose, and the separation is the
        feature: HITL that is only approve-or-deny makes a bot a vending
        machine. This carries a sentence, and it carries no authority — an open
        approval is still open after it, because a channel where discussion
        quietly authorises is worse than one with no discussion at all.
        """
        require_user(request, auth_provider)
        body = await request.json()
        return await _forward(
            "POST",
            "/say",
            data={"bot": str(body.get("bot", "")), "text": str(body.get("text", ""))},
        )

    @router.post("/bots/owner")
    async def sign(request: Request) -> dict[str, Any]:
        """
        Answer an owner-only verb — ``spend``, ``execute_order``,
        ``add_dependency``.

        A separate route from the one above rather than a flag on it. These
        are the three operations a bot's channel may never resolve, and the
        control plane mints a one-shot grant bound to the operation digest
        before it will record one. Sharing a handler would mean one missing
        field is the difference between the two authorities.

        ``confirmed`` must be explicitly true: the grant is a signature, and a
        signature nobody actively gave is the failure this path exists to
        prevent.
        """
        require_user(request, auth_provider)
        body = await request.json()
        return await _forward(
            "POST",
            "/owner/sign",
            data={
                "approval": str(body.get("approval", "")),
                "choice": str(body.get("choice", "")),
                "decision": "sign" if body.get("approved") else "refuse",
                "confirm": "yes" if body.get("confirmed") is True else "",
            },
        )

    @router.post("/bots/adopt")
    async def adopt(request: Request) -> dict[str, Any]:
        """
        Decide a bot that another bot proposed.

        Activation is the human act that keeps replication bounded, so it is a
        route rather than something a tick can do — and it is two acts, not
        one: ``draft`` creates the bot dormant, ``activate`` also switches it
        on. Anything unrecognised falls through to ``refuse``, because the safe
        reading of an unclear instruction about creating a bot is *no*.

        The caps — depth, fan-out, fleet size, and the allowance carved from
        the parent — are enforced in the store, not here.
        """
        require_user(request, auth_provider)
        body = await request.json()
        decision = str(body.get("decision", ""))
        return await _forward(
            "POST",
            "/spawn/adopt",
            data={
                "spawn": str(body.get("spawn", "")),
                "decision": decision if decision in ("activate", "draft") else "refuse",
                "because": str(body.get("because", "")),
            },
        )

    return router
