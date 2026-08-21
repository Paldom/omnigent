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

    def default_host(self) -> str | None:
        """
        Return an online host to pin sessions to, if there is one.

        A session with no host gets no runner, and every message to it is
        refused. Picking the first online host is right for the single-box
        deployment this is built for; name one explicitly in config if there
        is more than one.

        :returns: A host id, or ``None`` when no host is online.
        """
        hosts = self._request("GET", "/v1/hosts").get("hosts") or []
        for host in hosts:
            if host.get("status") == "online":
                return str(host["host_id"])
        return None

    def resolve_agent(self, name_or_id: str) -> str:
        """
        Turn an agent name into its id, passing an id straight through.

        Configuration names agents the way people do — ``claude``, ``marshal``
        — while the API wants the uuid it minted. Resolving here rather than
        making the operator paste ids keeps ``army.toml`` readable, and turns a
        typo into a clear error instead of a 404 from a session create.

        :param name_or_id: An agent name or an agent id.
        :returns: The agent id.
        :raises OmniError: If nothing matches.
        """
        agents = self._request("GET", "/v1/agents").get("data") or []
        for agent in agents:
            if agent.get("id") == name_or_id or agent.get("name") == name_or_id:
                return str(agent["id"])
        known = ", ".join(sorted(str(a.get("name")) for a in agents)) or "none"
        raise OmniError(f"no agent named {name_or_id!r}; this server has: {known}")

    def create_session(
        self,
        agent_id: str,
        *,
        title: str | None = None,
        harness: str | None = None,
        workspace: str | None = None,
        host_id: str | None = None,
    ) -> str:
        """
        Start a session and return its id.

        :param agent_id: Agent to bind.
        :param title: Name the session by what it is doing, not by its vendor —
            the vendor is an implementation detail that may change between
            iterations.
        :param harness: Per-session harness override, or ``None`` for the
            agent's own. Sent as ``harness_override``, which is the field the
            server validates — a plain ``harness`` key is accepted and ignored,
            so the session silently runs on the spec's harness instead.
        :param workspace: Working directory for the session, e.g. a worktree.
        :param host_id: Host to pin the session to. Without one no runner is
            bound, and every message to the session is refused with
            ``runner_unavailable`` — so an unattended loop needs this set.
        :returns: The new session id.
        """
        body: dict[str, Any] = {"agent_id": agent_id}
        if title is not None:
            body["title"] = title
        if harness is not None:
            body["harness_override"] = harness
        if workspace is not None:
            body["workspace"] = workspace
        if host_id is not None:
            body["host_id"] = host_id
        response = self._request("POST", "/v1/sessions", body)
        return str(response["id"])

    def send(self, session_id: str, text: str) -> None:
        """
        Send a message to a session.

        ``content`` is a list of typed parts, not a bare string — the same
        shape the UI posts, so an agent sees an identical message however it
        was sent.

        :param session_id: Session to send to.
        :param text: The message.
        """
        self._request(
            "POST",
            f"/v1/sessions/{session_id}/events",
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            },
        )

    def find_session(self, title: str) -> str | None:
        """
        Find an existing session by exact title.

        Lets a workload make session creation idempotent: derive a title from
        something stable about the iteration, look before creating, and a
        re-dispatch after a crash picks the session back up instead of leaving
        an orphan behind and starting a second one.

        :param title: The exact title to look for.
        :returns: The session id, or ``None`` when nothing matches.
        """
        response = self._request("GET", "/v1/sessions?limit=100")
        for session in response.get("data") or []:
            if session.get("title") == title:
                return str(session["id"])
        return None

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
        run_id: str,
        session_id: str,
        message: str,
        options: list[str],
        *,
        evidence: dict[str, Any] | None = None,
    ) -> str:
        """
        Put the iteration's question to the human, and return the barrier id.

        The barrier itself lives in this package, not in Omnigent, for two
        reasons that both point the same way.

        There is no client-initiated way to raise an elicitation: the session
        event API accepts ``approval`` and ``mcp_elicitation``, but both are
        *answers* to something a policy or an MCP server already asked. An
        external orchestrator has nothing to hook.

        And even if there were, it would be the wrong place. A policy ASK
        raised mid-turn is collapsed to DENY at every phase except INPUT
        (upstream #765), and a turn parked on one trips the harness idle
        watchdog (#4854). A barrier that has to survive until the next morning
        cannot live inside a turn.

        So what this does is post the question into the session as a message —
        so it is *visible* where the work happened, and answerable from the
        phone by replying — and hands back an id the run parks on. The
        authoritative answer arrives as a durable command, via ``army approve``.

        :param run_id: The run being parked, which the barrier id is derived
            from so it is stable across a restart.
        :param session_id: Session to post the question into.
        :param message: The question.
        :param options: The choices to offer.
        :param evidence: Anything the human should see before deciding.
        :returns: The barrier id to park the run on.
        """
        lines = [message, ""]
        if evidence:
            lines.append("Evidence:")
            lines += [f"  {key}: {value}" for key, value in sorted(evidence.items())]
            lines.append("")
        lines.append(f"Options: {', '.join(options)}")
        lines.append(f"Answer with: army approve {run_id[:12]} --choice <option>")
        self.send(session_id, "\n".join(lines))
        return f"barrier_{run_id}"
