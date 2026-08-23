"""Record the schema-form flow end to end, in a real browser.

Posts an ``mcp_elicitation`` the way the runner's callback does, then drives the
card: the form renders, Submit is refused while the required fields are blank,
and once they are answered the accept goes through and the prompt drains.
"""

import json
import os
import pty
import select
import subprocess
import sys
import time
import urllib.request

VENV = "/Users/dpal/Documents/Projects/omni-dev/omnigent-pr/.venv/bin/python"
BASE = "http://localhost:7799"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/uiproof/video"

SCHEMA = {
    "type": "object",
    "properties": {
        "branch": {"type": "string", "title": "Release branch"},
        "channel": {"type": "string", "enum": ["beta", "stable"]},
        "notify": {"type": "boolean", "title": "Notify the channel", "default": True},
    },
    "required": ["branch", "channel"],
}


def api(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        method="POST" if body else "GET",
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"} if body else {},
    )
    with urllib.request.urlopen(req, timeout=25) as f:
        return json.load(f)


# A live runner has to own the session, or the server has nothing to park on.
master, slave = pty.openpty()
proc = subprocess.Popen(
    [VENV, "-m", "omnigent", "run", "--harness", "claude-sdk", "--server", BASE],
    stdin=slave,
    stdout=slave,
    stderr=slave,
    cwd="/tmp/uiproof",
    close_fds=True,
)
os.close(slave)
buf = b""
deadline = time.time() + 110
while time.time() < deadline:
    r, _, _ = select.select([master], [], [], 1.0)
    if r:
        try:
            buf += os.read(master, 65536)
        except OSError:
            break
    if b"/help help" in buf:
        break
time.sleep(6)

sid = (api("/v1/sessions?limit=3").get("data") or [])[0]["id"]
print("session:", sid, flush=True)

from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context(
        viewport={"width": 1180, "height": 860},
        record_video_dir=OUT,
        record_video_size={"width": 1180, "height": 860},
    )
    pg = ctx.new_page()
    pg.goto(f"{BASE}/c/{sid}", wait_until="domcontentloaded", timeout=60000)
    pg.get_by_role("textbox", name="Message the agent").wait_for(state="visible", timeout=40000)
    pg.wait_for_timeout(2500)

    api(
        f"/v1/sessions/{sid}/events",
        {
            "type": "mcp_elicitation",
            "data": {"message": "Which release should I cut?", "requestedSchema": SCHEMA},
        },
    )

    form = pg.locator('[data-testid="elicitation-schema-form"]')
    form.wait_for(state="visible", timeout=30000)
    pg.wait_for_timeout(2500)

    submit = form.locator('[data-testid="elicitation-schema-submit"]')
    print("submit disabled while required fields blank:", submit.is_disabled(), flush=True)

    branch = form.locator('[data-testid="elicit-field-branch"] input[type="text"]')
    branch.click()
    for ch in "release/2.4":
        branch.type(ch, delay=90)
    pg.wait_for_timeout(1200)
    print("still disabled with one required field left:", submit.is_disabled(), flush=True)

    form.locator('[data-testid="elicit-field-channel"] select').select_option("stable")
    pg.wait_for_timeout(1500)
    print("enabled once both are answered:", submit.is_enabled(), flush=True)

    submit.click()
    pg.locator('[data-testid="approval-card"][data-state="responded"]').first.wait_for(
        state="visible", timeout=30000
    )
    pg.wait_for_timeout(3000)

    left = api(f"/v1/sessions/{sid}").get("pending_elicitations") or []
    print("pending elicitations after submit:", len(left), flush=True)

    ctx.close()
    browser.close()

proc.terminate()
print("video dir:", OUT, flush=True)
