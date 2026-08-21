import json, os, pty, select, subprocess, sys, time, urllib.request
VENV = "/Users/dpal/Documents/Projects/omni-dev/omnigent-pr/.venv/bin/python"
BASE = "http://localhost:7799"
LABEL = sys.argv[1]           # "before" | "after"
OUT = f"/tmp/webcheck/web-{LABEL}"

master, slave = pty.openpty()
p = subprocess.Popen([VENV, "-m", "omnigent", "run", "--harness", "claude-sdk", "--server", BASE],
                     stdin=slave, stdout=slave, stderr=slave, cwd="/tmp/webcheck", close_fds=True)
os.close(slave)
buf = b""; deadline = time.time() + 110
while time.time() < deadline:
    r, _, _ = select.select([master], [], [], 1.0)
    if r:
        try: buf += os.read(master, 65536)
        except OSError: break
    if b"/help help" in buf: break
time.sleep(7)
def api(path):
    with urllib.request.urlopen(BASE + path, timeout=25) as f:
        return json.load(f)
sid = (api("/v1/sessions?limit=3").get("data") or [])[0]["id"]

from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    b = pw.chromium.launch()
    ctx = b.new_context(viewport={"width": 1420, "height": 880},
                        record_video_dir=OUT, record_video_size={"width": 1420, "height": 880})
    pg = ctx.new_page()
    pg.goto(f"{BASE}/c/{sid}", wait_until="domcontentloaded", timeout=60000)
    ta = pg.get_by_label("Message the agent")
    ta.wait_for(state="visible", timeout=40000)
    pg.wait_for_timeout(3500)
    ta.click()
    for ch in "/figma":
        ta.type(ch, delay=110)
    pg.wait_for_timeout(4000)
    n = pg.locator('[data-testid^="slash-menu-item-"]').count()
    print(f"{LABEL}: menu items for '/figma' = {n}", flush=True)
    pg.screenshot(path=f"/tmp/webcheck/web-{LABEL}.png")
    pg.wait_for_timeout(2500)
    ctx.close(); b.close()
p.terminate()
try: p.wait(timeout=10)
except Exception: p.kill()
