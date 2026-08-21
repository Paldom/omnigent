"""A thin client for the Omnigent HTTP API.

The boundary between the two halves of the system. Omnigent owns sessions;
this package owns workflow state; everything that crosses between them goes
through here, so there is one place to look when the contract changes.

Deliberately small. It covers session create/send/get, reading a session's
inbox, and resolving an elicitation — the subset a supervisor needs. Anything
richer belongs in the generated SDK under ``sdks/``, not here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class OmniError(RuntimeError):
    """An Omnigent API call failed.

    :param message: What went wrong.
    :param status: HTTP status, when the call reached the server.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_transient(self) -> bool:
        """Whether retrying could plausibly help.

        A connection failure or a 5xx might; a 4xx means the request itself is
        wrong and will be wrong again.
        """
        return self.status is None or self.status >= 500 or self.status == 429


@dataclass(frozen=True)
class Session:
    """One Omnigent session, as much of it as the supervisor cares about.

    :param id: Session id.
    :param title: Human-facing name, e.g. ``"auth-refactor"``.
    :param status: Live turn status, e.g. ``"idle"`` / ``"running"``.
    :param pending_elicitations: Outstanding approval prompts on the session.
    """

    id: str
    title: str | None
    status: str | None
    pending_elicitations: list[dict[str, Any]]


class OmniClient:
    """Talks to one Omnigent server.

    Uses ``urllib`` rather than a dependency: the call volume is one request
    per supervisor decision, and a control plane that gates real money is a bad
    place to add packages it does not need.

    :param base_url: Server root, e.g. ``"http://localhost:6767"``.
    :param token: Bearer token for a server with auth enabled, or ``None``.
    :param timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:6767",
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """
        Issue one request and decode its JSON body.

        :param method: HTTP method.
        :param path: Path below the server root, starting with ``/``.
        :param body: JSON body, or ``None``.
        :returns: The decoded response, or ``None`` for an empty body.
        :raises OmniError: On any transport or HTTP failure.
        """
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise OmniError(f"{method} {path} -> {exc.code}: {detail}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise OmniError(f"{method} {path} -> {exc.reason}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise OmniError(f"{method} {path} returned a non-JSON body") from exc

    def health(self) -> bool:
        """
        Whether the server answers.

        :returns: ``True`` when ``/health`` responds, ``False`` otherwise.
        """
        try:
            self._request("GET", "/health")
        except OmniError:
            return False
        return True

    def create_session(
        self,
        agent_id: str,
        *,
        title: str | None = None,
        harness: str | None = None,
        workspace: str | None = None,
    ) -> str:
        """
        Start a session and return its id.

        :param agent_id: Agent to bind.
        :param title: Name the session by what it is doing, not by its vendor —
            the vendor is an implementation detail that may change between
            iterations.
        :param harness: Harness override, or ``None`` for the agent's own.
        :param workspace: Working directory for the session, e.g. a worktree.
        :returns: The new session id.
        """
        body: dict[str, Any] = {"agent_id": agent_id}
        if title is not None:
            body["title"] = title
        if harness is not None:
            body["harness"] = harness
        if workspace is not None:
            body["workspace"] = workspace
        response = self._request("POST", "/v1/sessions", body)
        return str(response["id"])

    def send(self, session_id: str, text: str) -> None:
        """
        Send a message to a session.

        :param session_id: Session to send to.
        :param text: The message.
        """
        self._request(
            "POST",
            f"/v1/sessions/{session_id}/events",
            {"type": "message", "data": {"role": "user", "content": text}},
        )

    def get_session(self, session_id: str) -> Session:
        """
        Read a session's current state.

        :param session_id: Session to read.
        :returns: The parts of the snapshot the supervisor uses.
        """
        data = self._request("GET", f"/v1/sessions/{session_id}")
        return Session(
            id=data.get("id", session_id),
            title=data.get("title"),
            status=data.get("status"),
            pending_elicitations=data.get("pending_elicitations") or [],
        )

    def resolve_elicitation(
        self,
        session_id: str,
        elicitation_id: str,
        *,
        action: str = "accept",
        content: dict[str, Any] | None = None,
    ) -> None:
        """
        Answer an outstanding approval.

        :param session_id: Session that raised it.
        :param elicitation_id: The prompt's correlation id.
        :param action: ``"accept"`` or ``"decline"``.
        :param content: Form values for a structured prompt, e.g. the option
            picked from a multi-select.
        """
        body: dict[str, Any] = {"action": action}
        if content is not None:
            body["content"] = content
        self._request(
            "POST",
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            body,
        )

    def ask(
        self,
        session_id: str,
        message: str,
        options: list[str],
        *,
        evidence: dict[str, Any] | None = None,
    ) -> str:
        """
        Raise an approval on a session and return its id.

        Sent as a message rather than a policy ASK on purpose. A policy ASK
        raised mid-turn is collapsed to DENY at every phase except INPUT
        (upstream #765), so a gate that depends on being *asked* must sit at a
        turn boundary, which is where the loop's iteration boundary already is.

        :param session_id: Session to ask on.
        :param message: The question.
        :param options: The choices to offer.
        :param evidence: Anything the human should see before deciding.
        :returns: The elicitation id to park the run on.
        """
        payload: dict[str, Any] = {"question": message, "options": options}
        if evidence:
            payload["evidence"] = evidence
        response = self._request(
            "POST",
            f"/v1/sessions/{session_id}/events",
            {"type": "elicitation", "data": payload},
        )
        if isinstance(response, dict) and response.get("elicitation_id"):
            return str(response["elicitation_id"])
        raise OmniError(f"session {session_id} did not return an elicitation id")
