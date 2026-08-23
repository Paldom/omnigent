"""Answer a barrier question in the Omnigent web UI, and watch the run advance.

The barrier's whole claim is that a question can wait in a session and be
answered from the browser you already have open. This drives that path for a
multi-select gate: the supervisor posts the question, a person types a reply in
the web chat, and the next tick picks it up.
"""

import json, sys, tempfile, time, urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, "/Users/dpal/Documents/Projects/omni-dev/omnigent-full")
from army.omni import OmniClient, barrier_marker      # noqa: E402
from army.state import Run, RunState                  # noqa: E402
from army.store import Store                          # noqa: E402
from army.supervisor import Supervisor                # noqa: E402

BASE = "http://localhost:7799"
REPLY = "popcorn and pretzels please"


class Snacks:
    """A gate whose options you may legitimately want several of."""

    name = "snacks"
    harness = None
    multi_select = True

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.items: list[dict[str, Any]] = [{"task": "movie night"}]

    def acquire(self) -> dict[str, Any] | None:
        return self.items.pop(0) if self.items else None

    def dispatch(self, run: Run, omni: OmniClient) -> list[str]:
        return [self.session_id]

    def collect(self, run: Run, omni: OmniClient) -> tuple[bool, dict[str, Any]]:
        return True, {"approval_session_id": self.session_id}

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:
        return ("Movie night — pick snacks", ["popcorn", "pretzels", "olives"], {})

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:
        if decision == "deny":
            return "paused", "declined"
        return "continue", f"chose {', '.join(payload.get('choices') or [])}"


def api(path: str) -> Any:
    with urllib.request.urlopen(BASE + path, timeout=25) as f:
        return json.load(f)


def _clean_session() -> str:
    """A session with nothing already awaiting an answer.

    A pending elicitation disables the composer — the barrier's point is that
    the question is an ordinary message you reply to, so the proof needs a
    session where replying is possible.
    """
    for s in api("/v1/sessions?limit=12").get("data") or []:
        snap = api(f"/v1/sessions/{s['id']}")
        if not (snap.get("pending_elicitations") or []) and snap.get("host_online"):
            return s["id"]
    raise SystemExit("no session without a pending elicitation")


sid = _clean_session()
omni = OmniClient(base_url=BASE)
tmp = tempfile.mkdtemp()
store = Store(Path(tmp) / "army.db")
workload = Snacks(sid)
sup = Supervisor(store, omni, workload)

for _ in range(6):
    sup.tick()
    runs = store.active_runs()
    if runs and runs[0].state is RunState.WAITING_HUMAN:
        break
run = store.active_runs()[0]
print(f"parked run {run.id[:12]} on session {sid[:12]} — question is in the session", flush=True)
print("marker:", barrier_marker(run.id), flush=True)

from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1180, "height": 900})
    pg.goto(f"{BASE}/c/{sid}", wait_until="domcontentloaded", timeout=60000)
    composer = pg.get_by_label("Message the agent")
    composer.wait_for(state="visible", timeout=40000)
    pg.wait_for_timeout(4000)
    pg.screenshot(path="/tmp/barrierui/ui-1-question.png")

    composer.click()
    composer.fill(REPLY)
    pg.wait_for_timeout(600)
    pg.screenshot(path="/tmp/barrierui/ui-2-typed.png")
    composer.press("Enter")
    pg.wait_for_timeout(3500)
    pg.screenshot(path="/tmp/barrierui/ui-3-sent.png")
    b.close()

print(f'reply "{REPLY}" sent from the browser', flush=True)
for _ in range(10):
    sup.tick()
    after = store.get_run(run.id)
    if after is not None and after.state is not RunState.WAITING_HUMAN:
        print(f"supervisor picked it up -> {after.state.value}: {after.terminal_reason}", flush=True)
        break
    time.sleep(1)
else:
    print("run is still parked", flush=True)
