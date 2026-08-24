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
    IDLE_CLOSE_S,
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


async def test_a_bot_cannot_type_into_a_password_field(gateway: BrowserGateway) -> None:
    """The one place "ask a person to sign in" stops being an instruction.

    A brief can be argued with by the page it is reading: text on the page
    claiming the operator already approved, or that this is a test environment,
    is exactly the shape of a successful prompt injection. A refusal in the
    executor is not persuadable.
    """
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    field = _ref(snapshot["tree"], "", kind="password")

    result = await gateway.perform(
        "test", "type", {"ref": field, "snapshot_id": snapshot["snapshot_id"], "text": "s3cret"}
    )

    assert result["ok"] is False
    assert "password field" in result["error"]
    assert "s3cret" not in str(result)


async def test_a_field_named_like_a_secret_is_refused_too(gateway: BrowserGateway) -> None:
    """``type="text"`` on something called ``otp`` is still a credential."""
    page = '<html><body><input type="text" name="otp_code" id="otp_code"></body></html>'
    await gateway.perform("test", "navigate", {"url": _url(page)})
    snapshot = await gateway.perform("test", "snapshot", {})
    field = _ref(snapshot["tree"], "", kind="text")

    result = await gateway.perform(
        "test", "type", {"ref": field, "snapshot_id": snapshot["snapshot_id"], "text": "123456"}
    )

    assert result["ok"] is False
    assert "123456" not in str(result)


async def test_an_ordinary_field_still_accepts_text_without_echoing_it(
    gateway: BrowserGateway,
) -> None:
    """The refusal must be narrow — a search box is not a credential."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    field = _ref(snapshot["tree"], "Search fees")

    result = await gateway.perform(
        "test", "type", {"ref": field, "snapshot_id": snapshot["snapshot_id"], "text": "tier 1"}
    )

    assert result["ok"] is True
    assert "tier 1" not in str(result), "a tool result is transcript"


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

    result = await gateway.perform(
        "test", "click", {"ref": ref, "snapshot_id": snapshot["snapshot_id"]}
    )

    assert result["ok"] is False
    assert "snapshot" in result["error"]
    assert "8000ms" not in result["error"], "a clear refusal, not an 8s selector timeout"


async def test_a_ref_cannot_smuggle_a_selector(gateway: BrowserGateway) -> None:
    """The ref is concatenated into a selector, so it must be digits."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})

    result = await gateway.perform(
        "test",
        "click",
        {"ref": '1"], button[aria-label="Reject All', "snapshot_id": snapshot["snapshot_id"]},
    )

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


@pytest.mark.parametrize(
    "name", ["../../../etc/evil", "acme/prod", "acme prod", "acme:prod", "..", "bot-\u0430"]
)
async def test_an_unusable_profile_name_is_refused_not_repaired(
    gateway: BrowserGateway, name: str
) -> None:
    """Sanitising a name silently merges identities.

    The first version replaced anything outside a small alphabet with ``_``, so
    ``acme/prod``, ``acme prod`` and ``acme_prod`` were one browser sharing one
    cookie jar — three identities, one session, and an audit trail that cannot
    say which of them did a thing. A Cyrillic lookalike did the same trick
    without any punctuation at all.
    """
    result = await gateway.perform(name, "navigate", {"url": _url(PAGE)})

    assert result["ok"] is False
    assert "not a usable browser profile name" in result["error"]
    assert [entry for entry in gateway.resident() if entry != "test"] == []


async def test_the_trail_records_what_the_browser_was_made_to_do(
    gateway: BrowserGateway,
) -> None:
    """A frame says where a browser is. Watching a bot work means seeing steps."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    await gateway.perform(
        "test",
        "click",
        {"ref": _ref(snapshot["tree"], "Reject All"), "snapshot_id": snapshot["snapshot_id"]},
    )

    trail = gateway.trail("test")
    assert [step["action"] for step in trail] == ["navigate", "snapshot", "click"]
    assert all(step["ok"] for step in trail)


async def test_the_trail_never_carries_typed_text(gateway: BrowserGateway) -> None:
    """The trail is shown in a UI; a password reaching it is a password in a screenshot."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    field = _ref(snapshot["tree"], "", kind="password")
    await gateway.perform(
        "test", "type", {"ref": field, "snapshot_id": snapshot["snapshot_id"], "text": "s3cret"}
    )

    assert "s3cret" not in str(gateway.trail("test"))
    assert "withheld" in gateway.trail("test")[-1]["target"]


async def test_a_failed_action_is_recorded_not_dropped(gateway: BrowserGateway) -> None:
    """A refused or wrong action is only explicable if the attempt was written down."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    await gateway.perform("test", "click", {"ref": 9999, "snapshot_id": snapshot["snapshot_id"]})

    last = gateway.trail("test")[-1]
    assert last["ok"] is False
    assert last["error"]


async def test_the_trail_is_bounded(gateway: BrowserGateway) -> None:
    """It lives as long as the browser does and nothing prunes it."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    for _ in range(TRAIL_LENGTH + 5):
        await gateway.perform("test", "snapshot", {})

    assert len(gateway.trail("test")) == TRAIL_LENGTH


# ── what a hostile page, or an agent talked into one, must not reach ──


@pytest.mark.parametrize(
    "url,expected",
    [
        # The profile root holds every other bot's cookie database. This is a
        # complete cross-bot credential theft wearing the shape of reading a page.
        ("file:///etc/passwd", "not reachable"),
        ("file:///Users/x/.omnigent/browser-profiles/bot-b/Default/Cookies", "not reachable"),
        # The bot's browser runs on the server, so loopback is the control
        # plane — the thing holding the fleet token and every open approval.
        ("http://127.0.0.1:6769/api/bots", "this machine"),
        ("http://localhost:6767/v1/sessions", "this machine"),
        # Cloud metadata: instance credentials over plain HTTP.
        ("http://169.254.169.254/latest/meta-data/iam/", "private network"),
        ("http://192.168.1.1/", "private network"),
        ("chrome://settings/passwords", "not reachable"),
    ],
)
async def test_a_bot_browser_refuses_to_leave_the_web(
    gateway: BrowserGateway, url: str, expected: str
) -> None:
    """Navigation is not a filesystem or an intranet scanner."""
    result = await gateway.perform("test", "navigate", {"url": url})

    assert result["ok"] is False
    assert expected in result["error"]


async def test_a_raw_selector_is_refused(gateway: BrowserGateway) -> None:
    """A selector walks past every control the ref path provides.

    `input[type=password]` picks exactly the field the brief forbids, and
    Playwright's `>>` and `xpath=` reach into frames the snapshot never showed
    — none of it visible in what the operator can read.
    """
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    await gateway.perform("test", "snapshot", {})

    result = await gateway.perform(
        "test", "type", {"selector": "input[type=password]", "text": "hunter2"}
    )

    assert result["ok"] is False
    assert "browser_snapshot" in result["error"]
    assert "hunter2" not in str(result)


async def test_the_page_cannot_move_a_ref_onto_something_else(
    gateway: BrowserGateway,
) -> None:
    """The ref lives in the page's own DOM, so the page can relocate it.

    A snapshot names ref N as a harmless button; page script then moves the
    attribute onto "Confirm transfer". Every other check passes — the
    snapshot_id is current, the ref is a digit — and the click lands on the
    attacker's element while the transcript still says it was the harmless one.
    """
    hostile = """
    <html><body>
      <button id="safe" aria-label="Read more">Read more</button>
      <button id="danger" aria-label="Confirm transfer">Confirm transfer</button>
    </body></html>
    """
    await gateway.perform("test", "navigate", {"url": _url(hostile)})
    snapshot = await gateway.perform("test", "snapshot", {})
    ref = _ref(snapshot["tree"], "Read more")

    # The page, reacting to having been snapshotted.
    await gateway._profiles["test"].page.evaluate(
        """() => {
            const safe = document.querySelector('#safe');
            const danger = document.querySelector('#danger');
            danger.setAttribute('data-omni-ref', safe.getAttribute('data-omni-ref'));
            safe.removeAttribute('data-omni-ref');
        }"""
    )

    result = await gateway.perform(
        "test", "click", {"ref": ref, "snapshot_id": snapshot["snapshot_id"]}
    )

    assert result["ok"] is False
    assert "moved it" in result["error"]
    assert "Confirm transfer" in result["error"]


async def test_the_trail_does_not_record_an_oauth_code(gateway: BrowserGateway) -> None:
    """A callback URL after a human signs in is a live credential."""
    await gateway.perform(
        "test",
        "navigate",
        {"url": "https://app.example/callback?code=SplxlOBeZQQYbYS6WxSbIA&state=xyz"},
    )

    trail = str(gateway.trail("test"))
    assert "SplxlOBeZQQYbYS6WxSbIA" not in trail
    assert "withheld" in trail


async def test_screenshots_are_not_readable_by_another_bot(gateway: BrowserGateway) -> None:
    """A picture of a logged-in page is the session. It lives under its own profile."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    result = await gateway.perform("test", "screenshot", {})

    assert Path(result["path"]).parent.parent.name == "test"
    assert "_shots" not in result["path"], "one shared folder is every bot's sessions"


async def test_a_page_cannot_hide_a_password_field_by_patching_its_own_dom(
    gateway: BrowserGateway,
) -> None:
    """The check must not run where the page can rewrite it.

    `page.evaluate` executes in the page's own JavaScript world, so a single
    line — `Element.prototype.getAttribute = () => 'text'` — makes every
    password box report as a text box. Playwright's locator API answers from an
    isolated world instead, which is the whole reason to use it here.
    """
    hostile = """
    <html><body>
      <input id="p" type="password" name="password">
      <script>
        const real = Element.prototype.getAttribute;
        Element.prototype.getAttribute = function (name) {
          if (name === 'type') return 'text';
          if (name === 'name') return 'nickname';
          return real.call(this, name);
        };
      </script>
    </body></html>
    """
    await gateway.perform("test", "navigate", {"url": _url(hostile)})
    snapshot = await gateway.perform("test", "snapshot", {})
    ref = int(snapshot["tree"].splitlines()[0].rsplit("[ref=", 1)[1].rstrip("]"))

    result = await gateway.perform(
        "test", "type", {"ref": ref, "snapshot_id": snapshot["snapshot_id"], "text": "hunter2"}
    )

    assert result["ok"] is False
    assert "hunter2" not in str(result)


async def test_a_one_time_code_field_is_a_credential(gateway: BrowserGateway) -> None:
    """2FA prompts are `type="text"` because that is what phone keyboards want.

    Filtering on `type="password"` alone misses every one of them — which is
    precisely the field an injected agent would be steered towards.
    """
    page = (
        '<html><body><input type="text" inputmode="numeric" '
        'autocomplete="one-time-code" name="code"></body></html>'
    )
    await gateway.perform("test", "navigate", {"url": _url(page)})
    snapshot = await gateway.perform("test", "snapshot", {})
    ref = int(snapshot["tree"].splitlines()[0].rsplit("[ref=", 1)[1].rstrip("]"))

    result = await gateway.perform(
        "test", "type", {"ref": ref, "snapshot_id": snapshot["snapshot_id"], "text": "483920"}
    )

    assert result["ok"] is False
    assert "483920" not in str(result)


async def test_a_page_that_moves_on_its_own_invalidates_its_refs(
    gateway: BrowserGateway,
) -> None:
    """Refs died only on the `navigate` action, not on how pages actually move.

    A click-through, a 302 and a meta refresh all left the snapshot id current
    and the stamps sitting on a document that no longer exists. Driven here by
    navigating the page underneath the gateway, so only the framenavigated
    listener can clear anything — the `navigate` action clears refs itself and
    would prove nothing.
    """
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    ref = _ref(snapshot["tree"], "Reject All")

    await gateway._profiles["test"].page.goto(
        _url("<html><body><h1>somewhere else</h1></body></html>")
    )

    result = await gateway.perform(
        "test", "click", {"ref": ref, "snapshot_id": snapshot["snapshot_id"]}
    )
    assert result["ok"] is False
    assert "snapshot" in result["error"]


async def test_two_snapshots_never_share_an_id(gateway: BrowserGateway) -> None:
    """A counter restarts at one when a profile relaunches.

    So `snap_1` taken before an eviction matched `snap_1` taken after it — the
    staleness check passing across a different browser and a different page.
    """
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    first = (await gateway.perform("test", "snapshot", {}))["snapshot_id"]
    await gateway.close("test")
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    second = (await gateway.perform("test", "snapshot", {}))["snapshot_id"]

    assert first != second


async def test_eviction_does_not_close_a_browser_that_is_in_use(tmp_path: Path) -> None:
    """A third bot waking up must not close a page another bot is mid-click on.

    It used to: eviction picked the least recently used and closed it without
    regard for the lock, so the victim got `Target closed` with no hint its
    browser had been taken, and its next action silently relaunched a blank
    page with the trail and the tab gone.
    """
    gateway = BrowserGateway(root=tmp_path, max_resident=1)
    try:
        busy = await gateway._profile_for("busy")
    except BrowserUnavailable as exc:
        pytest.skip(f"no browser available: {exc}")

    async with busy.lock:
        with pytest.raises(BrowserUnavailable, match="busy"):
            await gateway._profile_for("newcomer")
        assert "busy" in gateway.resident(), "the in-use browser survived"

    await gateway.shutdown()


# ── the wheel, as an interlock rather than a courtesy ────────────


async def test_a_held_browser_refuses_the_bot(gateway: BrowserGateway) -> None:
    """The refusal happens next to the browser, not over HTTP before it."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    gateway.hold("test", seconds=900)

    result = await gateway.perform("test", "snapshot", {})

    assert result["ok"] is False
    assert "taken the wheel" in result["error"]
    assert "not queued" in result["error"]


async def test_a_read_is_refused_too_while_a_person_drives(gateway: BrowserGateway) -> None:
    """A snapshot taken while somebody types a password transcribes it.

    Reads are the dangerous direction here, not writes — which is why the old
    version's fail-open-on-error was backwards: the moment the control plane
    was unreachable was the moment the wheel stopped protecting anything.
    """
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    gateway.hold("test", seconds=900)

    for action in ("snapshot", "screenshot", "navigate"):
        result = await gateway.perform("test", action, {"url": _url(PAGE)})
        assert result["ok"] is False, action


async def test_a_hold_can_be_taken_before_the_browser_exists(gateway: BrowserGateway) -> None:
    """Somebody takes the wheel of a bot that has not browsed yet."""
    gateway.hold("not-open-yet", seconds=900)
    assert gateway.driven_by_a_person("not-open-yet") is True


async def test_handing_back_lets_the_bot_drive_again(gateway: BrowserGateway) -> None:
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    gateway.hold("test", seconds=900)
    gateway.release("test")

    assert (await gateway.perform("test", "snapshot", {}))["ok"] is True


async def test_a_closed_laptop_is_not_a_stuck_browser(gateway: BrowserGateway) -> None:
    """A time, not a flag: a control plane that dies mid-hold must not brick it."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    gateway.hold("test", seconds=0)

    assert gateway.driven_by_a_person("test") is False
    assert (await gateway.perform("test", "snapshot", {}))["ok"] is True


async def test_the_refusal_is_recorded_so_the_gap_is_explicable(
    gateway: BrowserGateway,
) -> None:
    """A refused action is otherwise a mysterious hole in an iteration."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    gateway.hold("test", seconds=900)
    await gateway.perform("test", "snapshot", {})

    assert gateway.trail("test")[-1]["ok"] is False
    assert "wheel" in gateway.trail("test")[-1]["error"]


# ── the things that were documentation rather than behaviour ─────


async def test_an_idle_browser_is_actually_closed(tmp_path: Path) -> None:
    """`IDLE_CLOSE_S` had no caller, so it described nothing.

    A logged-in browser stayed resident for as long as the server ran. An
    uncalled reaper reads exactly like a working one to anybody skimming.
    """
    gateway = BrowserGateway(root=tmp_path)
    try:
        await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    except Exception:
        pytest.skip("no browser available")
    assert "test" in gateway.resident()

    gateway._profiles["test"].last_used -= IDLE_CLOSE_S + 1
    await gateway.sweep()

    assert gateway.resident() == []
    await gateway.shutdown()


async def test_sweeping_does_not_close_a_browser_in_use(gateway: BrowserGateway) -> None:
    """Same rule as eviction: idle-looking is not the same as idle."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    entry = gateway._profiles["test"]
    entry.last_used -= IDLE_CLOSE_S + 1

    async with entry.lock:
        await gateway.sweep()

    assert "test" in gateway.resident()


async def test_a_snapshot_admits_the_frames_it_cannot_read(gateway: BrowserGateway) -> None:
    """Consent, OAuth and payment forms are always in an iframe.

    The snapshot reads the main document only, and said `truncated: false` —
    so an agent looking for the sign-in button concluded it did not exist and
    looped, which is the failure the truncation flag exists to prevent.
    """
    page = f'<html><body><h1>Outer</h1><iframe src="{_url(PAGE)}"></iframe></body></html>'
    await gateway.perform("test", "navigate", {"url": _url(page)})

    snapshot = await gateway.perform("test", "snapshot", {})

    assert snapshot["hidden_frames"] == 1
    assert "not read" in snapshot["note"]


async def test_a_page_with_no_frames_says_nothing_about_them(gateway: BrowserGateway) -> None:
    """The note must not be noise on the ordinary case."""
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})

    assert snapshot["hidden_frames"] == 0
    assert snapshot["note"] == ""


async def test_a_frame_is_captured_even_when_the_action_failed(
    gateway: BrowserGateway,
) -> None:
    """A hung click left the last *good* picture on screen.

    Which is the most misleading thing the panel could show: the watcher sees a
    working page at exactly the moment the bot is stuck on a broken one.
    """
    await gateway.perform("test", "navigate", {"url": _url(PAGE)})
    snapshot = await gateway.perform("test", "snapshot", {})
    before = gateway._profiles["test"].frame

    await gateway._profiles["test"].page.goto(
        _url("<html><body><h1>a different page entirely</h1></body></html>")
    )
    await gateway.perform(
        "test", "click", {"ref": _ref(snapshot["tree"], "Reject All"), "snapshot_id": "stale"}
    )

    assert gateway._profiles["test"].frame != before


async def test_a_browser_with_nothing_drawn_says_so(tmp_path: Path) -> None:
    """`ok` with a null image left the panel showing an empty box."""
    gateway = BrowserGateway(root=tmp_path)
    try:
        await gateway._profile_for("blank")
    except BrowserUnavailable:
        pytest.skip("no browser available")

    frame = await gateway.frame("blank", fresh=False)

    assert frame["ok"] is False
    assert "not loaded a page" in frame["error"]
    await gateway.shutdown()


async def test_a_cap_of_zero_does_not_wedge_the_gateway(tmp_path: Path) -> None:
    """`min()` on an empty sequence, in a while loop that never ends."""
    gateway = BrowserGateway(root=tmp_path, max_resident=0)
    assert gateway.max_resident >= 1
