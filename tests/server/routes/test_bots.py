"""The ``/v1/bots`` proxy.

What is worth pinning here is the boundary, not the payloads. The route holds
a token the browser must never see, it forwards a verdict rather than deciding
one, and a control plane that is not running is a normal answer rather than a
failure — the page renders "not started" from it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.routes import bots as bots_route


@pytest.fixture
def token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A control plane that has run at least once."""
    path = tmp_path / "web-token"
    path.write_text("s3cret\n")
    monkeypatch.setattr(bots_route, "TOKEN_FILE", path)
    return path


@pytest.fixture
def client() -> TestClient:
    """An app carrying only this router."""
    app = FastAPI()
    app.include_router(bots_route.create_bots_router(), prefix="/v1")
    return TestClient(app)


def _transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    """Answer the control plane's port from memory, recording what was sent."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, **kwargs: Any) -> None:
        real_init(self, transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    return seen


def test_says_not_running_when_bot_mode_never_started(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No token on disk is a first-run state, not an error."""
    monkeypatch.setattr(bots_route, "TOKEN_FILE", tmp_path / "absent")
    body = client.get("/v1/bots").json()
    assert body == {
        "running": False,
        "reason": "Bot mode has not been started on this machine.",
    }


def test_says_not_running_when_the_loop_is_down(
    client: TestClient, token: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused connection names the address, so the fix is obvious."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    _transport(monkeypatch, refuse)
    body = client.get("/v1/bots").json()
    assert body["running"] is False
    assert "127.0.0.1:6768" in body["reason"]


def test_sends_the_token_and_never_returns_it(
    client: TestClient, token: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server holds the secret; the browser gets the data."""
    seen = _transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"bots": [{"slug": "scout"}]}),
    )
    body = client.get("/v1/bots").json()

    assert seen[0].headers["authorization"] == "Bearer s3cret"
    assert body == {"bots": [{"slug": "scout"}], "running": True}
    assert "s3cret" not in str(body)


def test_forwards_a_verdict_to_the_bound_path(
    client: TestClient, token: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Forwarded, not reimplemented.

    The action hash, policy version and run version are checked once, in the
    control plane that owns them — so this page cannot become a softer route
    to a verdict than the CLI.
    """
    seen = _transport(
        monkeypatch,
        lambda request: httpx.Response(303, headers={"location": "/bot/scout"}),
    )
    body = client.post(
        "/v1/bots/verdict",
        json={"approval": "a" * 32, "choice": "merge", "approved": True},
    ).json()

    assert seen[0].url.path == "/verdict"
    assert b"decision=approve" in seen[0].content
    assert body == {"running": True, "ok": True, "location": "/bot/scout"}


def test_a_refused_verdict_is_not_reported_as_success(
    client: TestClient, token: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control plane signals refusal in the redirect it chose."""
    _transport(
        monkeypatch,
        lambda request: httpx.Response(303, headers={"location": "/bot/scout?err=run+moved+on"}),
    )
    body = client.post(
        "/v1/bots/verdict",
        json={"approval": "a" * 32, "choice": "merge", "approved": True},
    ).json()
    assert body["ok"] is False


def test_denying_is_forwarded_as_a_denial(
    client: TestClient, token: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ``approved`` must never read as approval."""
    seen = _transport(monkeypatch, lambda request: httpx.Response(303, headers={"location": "/"}))
    client.post("/v1/bots/verdict", json={"approval": "a" * 32, "choice": ""})
    assert b"decision=deny" in seen[0].content
