"""Browser action bridge routes.

Two executors behind one tool surface. A person's conversation is served by the
desktop app's embedded browser, claimed by whichever Electron window wins the
race. A bot's session is served by the server-owned browser gateway, because a
background fleet has no subscribed renderer and its actions would otherwise sit
until they timed out.

Which one runs is decided by a label the bot's control plane sets when it opens
the session. Nothing here imports that control plane — the label is the whole
contract, and the wheel is asked over the same loopback boundary the bots proxy
already uses.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

from fastapi import (
    APIRouter,
    Request,
)

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import (
    session_stream,
)
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_EDIT,
    AuthProvider,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._sessions.common import (
    _BROWSER_ACTION_TIMEOUT_RESULT,
    _browser_action_claims,
    _browser_action_owners,
    _browser_action_registry,
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.schemas import (
    BrowserActionRequestEvent,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

#: The session label a bot's control plane sets to name its browser profile.
#: Its presence is what routes an action to the gateway instead of the desktop.
_BROWSER_PROFILE_LABEL = "omnigent.browser.profile"

#: How stale the gateway's copy of who-holds-the-wheel may get. Small enough
#: that a phone taking the wheel stops the bot before the next page loads,
#: large enough that it is not a round trip per click.
WHEEL_SYNC_S = 5.0

#: When each profile last agreed with the ledger.
_wheel_synced_at: dict[str, float] = {}

_logger = logging.getLogger(__name__)


def register_browser_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the browser routes on router."""

    def _gateway_profile(store: Any, session_id: str) -> str | None:
        """
        The browser profile this session should drive, if it is a bot's.

        Read from the session's labels rather than by asking Bot mode, so the
        hot path is a store read the route already has rights to and the
        server keeps importing nothing from ``army``.

        Deliberately not wrapped in ``except Exception``. A broad catch here
        reads every failure as "not a bot" and silently sends the action to a
        desktop renderer that does not exist, where it dies as a 30s timeout —
        which is exactly how a wrong method name survived a live run.

        :param store: The conversation store.
        :param session_id: The session.
        :returns: The profile name, or ``None`` for an ordinary conversation.
        """
        conversation = store.get_conversation(session_id)
        labels = getattr(conversation, "labels", None) or {}
        profile = labels.get(_BROWSER_PROFILE_LABEL)
        return str(profile) if profile else None

    async def _sync_wheel_with_the_ledger(gateway: Any, profile: str) -> None:
        """
        Keep the gateway's hold in step with the durable ledger.

        The gateway's own hold is what refuses an action, and it is set by the
        proxy route when somebody takes the wheel *through this server*. The
        standalone control-plane page — the one that opens on a phone over the
        tailnet, which is the whole reason the approval path works from one —
        writes only to SQLite. So taking the wheel from your phone left the bot
        driving, and handing it back through this server while the phone still
        held it would have been just as wrong in the other direction.

        Re-asked at most every :data:`WHEEL_SYNC_S`, not per action: per-action
        was an HTTP round trip in front of every click, and the window it left
        between answering and acting was the bug that moved the hold in here in
        the first place. Between syncs the local value stands, so the worst case
        is a few seconds of staleness rather than never noticing.

        An unreachable control plane leaves the last known answer alone. That is
        what keeps failing open safe: it cannot talk the gateway out of a
        refusal it already knows about, it can only fail to tell it about a new
        one.

        :param gateway: The browser gateway.
        :param profile: The browser about to be driven.
        """
        now = time.monotonic()
        if now - _wheel_synced_at.get(profile, 0.0) < WHEEL_SYNC_S:
            return
        try:
            from omnigent.server.routes.bots import WHEEL_LEASE_S, wheel_refusal_for_profile

            held = bool(await wheel_refusal_for_profile(profile))
        except Exception:
            _logger.debug("could not read the wheel ledger for %s", profile, exc_info=True)
            return
        _wheel_synced_at[profile] = now
        if held:
            gateway.hold(profile, seconds=WHEEL_LEASE_S)
        else:
            gateway.release(profile)

    @router.post(
        "/sessions/{session_id}/browser/action_request",
        # Internal embedded-browser flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def browser_action_request(
        request: Request,
        session_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Park one embedded-browser action and await the renderer result.

        Mints an ``action_id``, parks a Future owned by ``session_id``, publishes
        a ``browser.action_request`` event, and awaits up to
        ``_BROWSER_ACTION_AWAIT_S``; on timeout returns the timeout result (HTTP
        200) so the runner gets a clean tool error. Called by the runner's
        ``browser_*`` dispatch, not the LLM.

        :param request: The inbound request, used for identity extraction.
        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param body: ``{"action": <str>, "args": <dict>}`` where ``action``
            is the ``browser_`` tool name minus the prefix.
        :returns: The renderer's action-result JSON, or the timeout result.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        action = body.get("action")
        args = body.get("args")
        if not isinstance(action, str) or not action:
            raise OmnigentError(
                "browser action_request requires a non-empty 'action'",
                code=ErrorCode.INVALID_INPUT,
            )
        if not isinstance(args, dict):
            args = {}

        # A bot's session names a browser profile in its labels, and that is
        # the whole routing decision: the desktop relay executes for a person's
        # conversation, the server-owned gateway executes for a bot's. Same
        # tool, same arguments, same result shape — a different executor,
        # because a background fleet has no subscribed renderer and its actions
        # would otherwise sit until they timed out.
        profile = _gateway_profile(conversation_store, session_id)
        if profile is not None:
            # Whether a person holds the wheel is decided inside the gateway,
            # next to the browser and inside the lock that serialises actions
            # on it. Asking the control plane here instead meant an HTTP round
            # trip that failed open on every error, followed by up to
            # twenty-five seconds of action — so a person could take the wheel
            # and still have the bot snapshot the form they were typing into.
            from omnigent.browser import gateway as _browser_gateway

            await _sync_wheel_with_the_ledger(_browser_gateway(), profile)
            return await _browser_gateway().perform(profile, action, args)

        action_id = f"baction_{secrets.token_hex(16)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        _browser_action_registry[action_id] = future
        _browser_action_owners[action_id] = session_id
        try:
            event = BrowserActionRequestEvent(
                type="browser.action_request",
                action_id=action_id,
                action=action,
                args=args,
            )
            from omnigent.server.routes import sessions as _sessions_facade

            _sessions_facade.session_stream.publish(session_id, event.model_dump())
            done, _pending = await asyncio.wait(
                {future},
                timeout=_sessions_facade._BROWSER_ACTION_AWAIT_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if future in done and not future.cancelled():
                return future.result()
            # Timed out/cancelled with no renderer result (no subscribed app).
            return _BROWSER_ACTION_TIMEOUT_RESULT
        finally:
            # Drop registry entries so a resolved/timed-out action leaks nothing.
            if _browser_action_registry.get(action_id) is future:
                _browser_action_registry.pop(action_id, None)
            _browser_action_owners.pop(action_id, None)
            _browser_action_claims.pop(action_id, None)

    @router.post(
        "/sessions/{session_id}/browser/action_claim/{action_id}",
        # Internal embedded-browser flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def browser_action_claim(
        request: Request,
        session_id: str,
        action_id: str,
    ) -> dict[str, Any]:
        """
        Atomically claim a parked browser action (one winner per action).

        The request event fans out to every subscribed renderer; an atomic
        ``setdefault`` grants exactly one claim so they don't double-execute.
        Winner gets ``{"claimed": true, "claim_token": <token>}``; everyone
        else ``{"claimed": false}``.

        :param request: The inbound request, used for identity extraction.
        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param action_id: The action to claim, e.g. ``"baction_abc123"``.
        :returns: ``{"claimed": true, "claim_token": <str>}`` to the winner,
            ``{"claimed": false}`` to losers or for an unknown/expired action.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        # Unknown / already-resolved action: nothing to claim.
        if _browser_action_owners.get(action_id) != session_id:
            return {"claimed": False}
        # Single-winner lease via atomic setdefault: a losing racer sees the
        # winner's token, not its own, and bails.
        claim_token = secrets.token_hex(16)
        existing = _browser_action_claims.setdefault(action_id, claim_token)
        if existing != claim_token:
            return {"claimed": False}
        return {"claimed": True, "claim_token": claim_token}

    @router.post(
        "/sessions/{session_id}/browser/action_result/{action_id}",
        # Internal embedded-browser flow — hidden from the public API reference.
        include_in_schema=False,
        status_code=202,
        response_model=None,
    )
    async def browser_action_result(
        request: Request,
        session_id: str,
        action_id: str,
        body: dict[str, Any],
    ) -> dict[str, bool]:
        """
        Deliver a browser action result, resolving the parked Future.

        Guarded by owner + claim-token: the caller must present the token this
        action was leased under, so a renderer that lost the claim race can't
        resolve the Future with stale work (tokenless/mismatched → 403).

        :param request: The inbound request, used for identity extraction.
        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param action_id: The action being resolved, e.g. ``"baction_abc"``.
        :param body: ``{"result": <dict>, "claim_token": <str>}``.
        :returns: ``{"resolved": true}`` when the Future was set,
            ``{"resolved": false}`` when it was already done/gone.
        :raises OmnigentError: 404 if no session exists; 403 on a missing or
            mismatched claim token or an owner mismatch.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        claim_token = body.get("claim_token")
        expected = _browser_action_claims.get(action_id)
        if not isinstance(claim_token, str) or expected is None or claim_token != expected:
            raise OmnigentError(
                "browser action result requires a matching claim_token",
                code=ErrorCode.FORBIDDEN,
            )
        # Only the session that issued the action may resolve it.
        if _browser_action_owners.get(action_id) != session_id:
            raise OmnigentError(
                "browser action is not owned by this session",
                code=ErrorCode.FORBIDDEN,
            )
        future = _browser_action_registry.get(action_id)
        if future is None or future.done():
            return {"resolved": False}
        result = body.get("result")
        future.set_result(result if isinstance(result, dict) else {"result": result})
        return {"resolved": True}
