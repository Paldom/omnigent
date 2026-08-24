"""The server-owned browser, driven against a real page.

Uses Playwright against a ``data:`` URL rather than a mock, because every bug
these tests exist for was in the *page* half of the contract: what a snapshot
can name, what a click can find, and how big a result gets before it stops
fitting in a model's context.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from omnigent.browser.gateway import (
    MAX_REFS,
    TRAIL_LENGTH,
    BrowserGateway,
    BrowserUnavailable,
)

pytestmark = pytest.mark.asyncio

PAGE = """
<html><body>
  <h1>Fee schedule</h1>
  <details><summary id="s">Spot crypto</summary>
    <table><tr><td>Tier 1</td><td>0.40%</td><td>0.80%</td></tr></table>
  </details>
  <button aria-label="Reject All">Reject</button>
  <input type="password" value="hunter2">
  <input type="text" placeholder="Search fees">
  <span>not interactive</span>
</body></html>
"""


def _ref(tree: str, name: str, *, kind: str | None = None) -> int:
    """Pull a ref out of the ``[ref=N]`` listing, the way an agent reads it."""
    for line in tree.splitlines():
        if (f'"{name}"' in line if name else True) and (
            kind is None or line.startswith(f"- {kind} ")
        ):
            return int(line.rsplit("[ref=", 1)[1].rstrip("]"))
    raise AssertionError(f"no {kind or ''} {name!r} in:\n{tree}")


def _url(html: str) -> str:
    return "data:text/html;base64," + base64.b64encode(html.encode()).decode()


@pytest.fixture
async def gateway(tmp_path: Path) -> Any:
    gw = BrowserGateway(root=tmp_path)
    try:
        await gw._profile_for("test")
    except BrowserUnavailable as exc:
        pytest.skip(f"no browser available: {exc}")
    yield gw
    await gw.shutdown()


async def test_snapshot_names_what_can_be_clicked(gateway: BrowserGateway) -> None:
    """A snapshot without refs leaves an agent guessing CSS selectors.

    The first live watcher run got text-only snapshots, could not open the
    collapsed fee table, and fell back to fetching the HTML with ``curl`` —
    which works, and means the browser was decoration.
    """
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})

    assert snapshot["ok"] is True
    assert "Fee schedule" in snapshot["text"]
    assert snapshot["snapshot_id"].startswith("snap_")
    tree = snapshot["tree"]
    assert '"Reject All"' in tree, "an aria-label is the best name a control has"
    assert '"Spot crypto"' in tree, "a <summary> is how a collapsed table opens"
    assert "[ref=" in tree, "the schema promises the [ref=N] form"
    assert snapshot["truncated"] is False


async def test_a_ref_from_the_snapshot_is_clickable(gateway: BrowserGateway) -> None:
    """The ref round-trip: name it in a snapshot, click it by name."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    ref = _ref(snapshot["tree"], "Spot crypto")

    result = await gateway.perform(
        "test", "click", {"ref": ref, "snapshot_id": snapshot["snapshot_id"]}
    )
    assert result["ok"] is True

    after = await gateway.perform("test", "snapshot", {})
    assert "0.80%" in after["text"], "the accordion should be open now"


async def test_a_snapshot_never_carries_a_password(gateway: BrowserGateway) -> None:
    """A filled password box must not have its value read back.

    A tool result is transcript: it lands in the run artifacts and anything
    downstream that reads them. Naming the field is useful; echoing it is a
    credential leak with extra steps.
    """
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})

    assert "hunter2" not in str(snapshot)
    assert "- password" in snapshot["tree"], "naming the field is fine; echoing it is not"


async def test_typing_a_secret_is_not_echoed_back(gateway: BrowserGateway) -> None:
    """``type`` confirms it acted without repeating what it typed."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    field = _ref(snapshot["tree"], "", kind="password")

    result = await gateway.perform("test", "type", {"ref": field, "text": "s3cret"})
    assert result["ok"] is True
    assert "s3cret" not in str(result)


async def test_screenshot_returns_a_path_not_sixty_kilobytes_of_base64(
    gateway: BrowserGateway,
) -> None:
    """A data URL in a tool result overran the model's output limit.

    60,481 characters, one tool call, and the agent spent two turns shelling
    out to read the image off disk itself. A path is what it wanted.
    """
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    result = await gateway.perform("test", "screenshot", {})

    assert result["ok"] is True
    assert "dataUrl" not in result
    image = Path(result["path"]).read_bytes()
    assert image[:2] == b"\xff\xd8", "a JPEG"
    # The page's own address is not payload — this page happens to *be* a data
    # URL. What must not ride along is the image.
    carried = {key: value for key, value in result.items() if key != "url"}
    assert len(str(carried)) < 400
    assert base64.b64encode(image).decode()[:40] not in str(result)


async def test_successive_screenshots_do_not_overwrite_each_other(
    gateway: BrowserGateway,
) -> None:
    """Before-and-after is the normal reason to take two."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    first = await gateway.perform("test", "screenshot", {})
    second = await gateway.perform("test", "screenshot", {})

    assert first["path"] != second["path"]
    assert Path(first["path"]).exists()


async def test_click_without_a_target_is_refused_not_guessed(
    gateway: BrowserGateway,
) -> None:
    """No ref and no selector is a caller bug; clicking something anyway is worse."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    result = await gateway.perform("test", "click", {})

    assert result["ok"] is False
    assert "ref" in result["error"]


async def test_the_frame_a_person_watches_is_the_page_the_agent_drove(
    gateway: BrowserGateway,
) -> None:
    """One page, two viewers — the guarantee that makes the wheel meaningful."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    frame = await gateway.frame("test", fresh=True)

    assert frame["ok"] is True
    assert frame["dataUrl"].startswith("data:image/jpeg;base64,")


@pytest.mark.parametrize("fresh", [False, True])
async def test_a_profile_that_was_never_opened_says_so(tmp_path: Path, fresh: bool) -> None:
    """Opening the panel must not launch a browser nobody asked for.

    Including with ``fresh``, which is what the panel actually passes. It used
    to mean "start one if there isn't one", so viewing an idle bot spent 300MB
    on a blank page — and at a resident cap of two, evicted the browser a
    working bot was using.
    """
    gateway = BrowserGateway(root=tmp_path)
    frame = await gateway.frame("never-opened", fresh=fresh)

    assert frame["ok"] is False
    assert "not open" in frame["error"]
    assert gateway.resident() == []


async def test_one_bot_per_profile_directory(gateway: BrowserGateway, tmp_path: Path) -> None:
    """A shared profile means one bot's compromise is every bot's session."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    assert (tmp_path / "test").is_dir()
    assert MAX_REFS > 0


async def test_a_ref_from_a_superseded_snapshot_is_refused(gateway: BrowserGateway) -> None:
    """Refs are renumbered per snapshot, so a stale one points somewhere plausible.

    Clicking it anyway is the worst outcome available: it succeeds, on the
    wrong element, and nothing in the transcript says so.
    """
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    first = await gateway.perform("test", "snapshot", {})
    ref = _ref(first["tree"], "Reject All")
    await gateway.perform("test", "snapshot", {})

    result = await gateway.perform(
        "test", "click", {"ref": ref, "snapshot_id": first["snapshot_id"]}
    )

    assert result["ok"] is False
    assert "superseded" in result["error"]


async def test_navigating_invalidates_every_ref(gateway: BrowserGateway) -> None:
    """A new document means the old refs describe elements that no longer exist."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    ref = _ref(snapshot["tree"], "Reject All")
    await gateway.perform("test", "navigate", {"url": _url("<html><body>gone</body></html>")})

    result = await gateway.perform("test", "click", {"ref": ref})

    assert result["ok"] is False
    assert "snapshot" in result["error"]
    assert "8000ms" not in result["error"], "a clear refusal, not an 8s selector timeout"


async def test_a_ref_cannot_smuggle_a_selector(gateway: BrowserGateway) -> None:
    """The ref is concatenated into a selector, so it must be digits."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    await gateway.perform("test", "snapshot", {})

    result = await gateway.perform("test", "click", {"ref": '1"], button[aria-label="Reject All'})

    assert result["ok"] is False
    assert "integer" in result["error"]


async def test_one_profile_under_two_spellings_is_one_browser(gateway: BrowserGateway) -> None:
    """``persist:bot-x`` and ``bot-x`` name the same browser.

    A bot definition writes the Electron partition form; the Screen panel asks
    for the bare slug. Keyed apart they would be two Chromiums contending for
    one profile directory's SingletonLock, and the panel would show the browser
    the agent was not driving.
    """
    await gateway.perform("persist:bot-kraken", "navigate", {"url": _url(PAGE)})
    frame = await gateway.frame("bot-kraken", fresh=False)

    assert frame["ok"] is True, "the panel must find the browser the agent opened"
    assert "bot-kraken" in gateway.resident()
    assert "persist:bot-kraken" not in gateway.resident(), "one entry, not two"


async def test_a_profile_name_cannot_choose_the_directory(gateway: BrowserGateway) -> None:
    """The name arrives from a session label and becomes a path."""
    await gateway.perform("../../../etc/evil", "navigate", {"url": _url(PAGE)})

    opened = [name for name in gateway.resident() if name != "test"]
    assert opened == ["_.._.._etc_evil"]


async def test_the_trail_records_what_the_browser_was_made_to_do(
    gateway: BrowserGateway,
) -> None:
    """A frame says where a browser is. Watching a bot work means seeing steps."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    await gateway.perform("test", "click", {"ref": _ref(snapshot["tree"], "Reject All")})

    trail = gateway.trail("test")
    assert [step["action"] for step in trail] == ["navigate", "snapshot", "click"]
    assert all(step["ok"] for step in trail)


async def test_the_trail_never_carries_typed_text(gateway: BrowserGateway) -> None:
    """The trail is shown in a UI; a password reaching it is a password in a screenshot."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    field = _ref(snapshot["tree"], "", kind="password")
    await gateway.perform("test", "type", {"ref": field, "text": "s3cret"})

    assert "s3cret" not in str(gateway.trail("test"))
    assert "withheld" in gateway.trail("test")[-1]["target"]


async def test_a_failed_action_is_recorded_not_dropped(gateway: BrowserGateway) -> None:
    """A refused or wrong action is only explicable if the attempt was written down."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    await gateway.perform("test", "click", {"selector": "#nothing-here"})

    last = gateway.trail("test")[-1]
    assert last["ok"] is False
    assert last["error"]


async def test_the_trail_is_bounded(gateway: BrowserGateway) -> None:
    """It lives as long as the browser does and nothing prunes it."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    for _ in range(TRAIL_LENGTH + 5):
        await gateway.perform("test", "snapshot", {})

    assert len(gateway.trail("test")) == TRAIL_LENGTH
