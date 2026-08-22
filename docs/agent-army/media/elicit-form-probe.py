"""Show the approval card for a real MCP-shaped requestedSchema, in the browser.

Posts an `mcp_elicitation` event the way the runner's callback does, then opens
the session and screenshots the card. The schema is the one an MCP server sends
when it needs fields, not consent.
"""
import json, os, pty, select, subprocess, sys, time, urllib.request

VENV = "/Users/dpal/Documents/Projects/omni-dev/omnigent-pr/.venv/bin/python"
BASE = "http://localhost:7799"
LABEL = sys.argv[1]

SCHEMA = {
    "type": "object",
    "properties": {
        "branch": {"type": "string", "title": "Release branch"},
        "channel": {"type": "string", "enum": ["beta", "stable"]},
        "notify": {"type": "boolean", "title": "Notify the channel", "default": True},
    },
    "required": ["branch", "channel"],
}

master, slave = pty.openpty()
p = subprocess.Popen([VENV, "-m", "omnigent", "run", "--harness", "claude-sdk", "--server", BASE],
                     stdin=slave, stdout=slave, stderr=slave, cwd="/tmp/uiproof", close_fds=True)
os.close(slave)
buf = b""; deadline = time.time() + 110
while time.time() < deadline:
    r, _, _ = select.select([master], [], [], 1.0)
    if r:
        try: buf += os.read(master, 65536)
        except OSError: break
    if b"/help help" in buf: break
time.sleep(6)

def api(path, body=None):
    req = urllib.request.Request(BASE + path, method="POST" if body else "GET",
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=25) as f:
        return json.load(f)

sid = (api("/v1/sessions?limit=3").get("data") or [])[0]["id"]
resp = api(f"/v1/sessions/{sid}/events", {
    "type": "mcp_elicitation",
    "data": {"message": "Which release should I cut?", "requestedSchema": SCHEMA},
})
print("elicitation:", resp.get("elicitation_id"), flush=True)

from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1180, "height": 900})
    pg.goto(f"{BASE}/c/{sid}", wait_until="domcontentloaded", timeout=60000)
    pg.get_by_label("Message the agent").wait_for(state="visible", timeout=40000)
    pg.wait_for_timeout(5000)
    form = pg.locator('[data-testid="elicitation-schema-form"]')
    print(f"{LABEL}: schema form present =", form.count(), flush=True)
    card = pg.locator('[data-testid="approval-card-options"], [role="alert"]').first
    try:
        card.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    pg.wait_for_timeout(1200)
    pg.screenshot(path=f"/tmp/uiproof/ui-{LABEL}.png")
    if form.count():
        submit = pg.locator('[data-testid="elicitation-schema-submit"]')
        print("   submit disabled while required blank:", submit.is_disabled(), flush=True)
        pg.locator("#elicit-field-branch").fill("release/2.4")
        pg.locator("#elicit-field-channel").select_option("stable")
        pg.wait_for_timeout(700)
        print("   submit enabled once answered:", submit.is_enabled(), flush=True)
        pg.screenshot(path=f"/tmp/uiproof/ui-{LABEL}-filled.png")
    b.close()

p.terminate()
try: p.wait(timeout=10)
except Exception: p.kill()
