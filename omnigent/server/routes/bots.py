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
