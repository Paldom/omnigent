/**
 * Records a walkthrough of the Bots web surface.
 *
 * Everything on screen is real: a live server, a real SQLite file, real
 * approvals answered through the same bound path the CLI uses. The only thing
 * added is the caption strip, injected as an overlay so the narration sits
 * beside the product rather than replacing it.
 */
import { execFileSync } from "node:child_process";
import pkg from "playwright-core";
const { chromium } = pkg;

const TOKEN = process.argv[2];
const PORT = process.argv[3];
const OUT = process.argv[4];
const BASE = `http://127.0.0.1:${PORT}`;

// A full Chromium, not the headless shell: the shell cannot record video.
// PLAYWRIGHT_CHROME points at one when the default resolution does not work.
const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROME || undefined,
});
const context = await browser.newContext({
  viewport: { width: 1280, height: 860 },
  deviceScaleFactor: 2,
  recordVideo: { dir: OUT, size: { width: 1280, height: 860 } },
});
const page = await context.newPage();

const CAPTION_CSS = `
#wt{position:fixed;left:0;right:0;bottom:0;z-index:99999;
  background:#11171cf2;color:#fff;padding:14px 22px 16px;
  font:500 15px/1.5 ui-sans-serif,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
  transform:translateY(110%);transition:transform .28s cubic-bezier(.2,.8,.2,1)}
#wt.on{transform:translateY(0)}
#wt b{display:block;font-size:11.5px;font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;color:#ffffff8c;margin-bottom:3px}
#wt em{font-style:normal;color:#ffffffb8}
`;

/** Put the caption strip on whatever page is loaded. */
async function mount() {
  await page.addStyleTag({ content: CAPTION_CSS });
  await page.evaluate(() => {
    if (document.getElementById("wt")) return;
    const el = document.createElement("div");
    el.id = "wt";
    document.body.appendChild(el);
  });
}

/** Show a caption, hold it, and optionally keep it up for the next step. */
async function say(kicker, text, hold = 3400) {
  await page.evaluate(
    ([k, t]) => {
      const el = document.getElementById("wt");
      el.innerHTML = `<b>${k}</b>${t}`;
      el.classList.add("on");
    },
    [kicker, text],
  );
  await page.waitForTimeout(hold);
}

async function hide() {
  await page.evaluate(() => document.getElementById("wt")?.classList.remove("on"));
  await page.waitForTimeout(400);
}

/** Scroll smoothly so the recording reads as a person looking down a page. */
async function glide(to, ms = 1400) {
  await page.evaluate(
    ([target, duration]) => {
      const start = window.scrollY;
      const delta = target - start;
      const t0 = performance.now();
      return new Promise((done) => {
        function step(t) {
          const p = Math.min(1, (t - t0) / duration);
          const e = 1 - Math.pow(1 - p, 3);
          window.scrollTo(0, start + delta * e);
          p < 1 ? requestAnimationFrame(step) : done();
        }
        requestAnimationFrame(step);
      });
    },
    [to, ms],
  );
}

async function visit(path) {
  await page.goto(`${BASE}${path}${path.includes("?") ? "&" : "?"}token=${TOKEN}`);
  await mount();
  await page.waitForTimeout(500);
}

// ── 1. the fleet ────────────────────────────────────────────────
await visit("/bots");
await say(
  "Omnigent · Bot mode",
  "Long-running bots that own no process between iterations. A bot is data; a body is disposable.",
  4200,
);
await say(
  "The roster",
  "Nine bots, nine operational statuses — none of them stored. Every one is computed from the runs, " +
    "the vendor gates and the clock, so it cannot disagree with what is actually happening.",
  5200,
);
await say(
  "Ordered by consequence",
  "What needs a person is at the top. The question the roster exists to answer at a glance is " +
    "<em>which one is stuck?</em>",
  4200,
);

// ── 2. the approval ─────────────────────────────────────────────
await say(
  "An approval is a row",
  "harvester finished an iteration and is waiting. The run holds no session, no process and no " +
    "vendor seat while it waits — a reboot here costs nothing.",
  5000,
);
await say(
  "Bound, not merely asked",
  "The card shows what the verdict is bound to: the verb, the evidence, and the action hash. " +
    "A human bound to arguments they were never shown is not bound to anything.",
  5400,
);
await hide();
await page.hover("text=continue");
await page.waitForTimeout(700);
await say(
  "Answering",
  "The same bound path the CLI uses. A verdict that no longer matches its question re-asks rather " +
    "than being honoured.",
  3800,
);
await hide();
await page.click("text=continue");
await page.waitForTimeout(900);
await mount();
await say(
  "Recorded, not applied",
  "Only a decision creates a Command, and recording it is all this does. The loop applies it on its " +
    "next tick — so answering while nothing is running is normal.",
  5000,
);

// A real tick, from a second process, against the same SQLite file the page is
// reading. Two writers, one database — which is the case the busy timeout and
// the single transaction policy exist for.
await say(
  "Meanwhile, the loop",
  "<em>army bots run --once</em> — a different process, the same file. The page is reading while " +
    "the supervisor writes.",
  3600,
);
execFileSync(
  "uv",
  ["run", "--frozen", "python", "-m", "army", "bots", "run", "--once", "--offline",
   "--config", process.env.ARMY_CONFIG],
  { stdio: "ignore" },
);
await hide();
await page.reload();
await mount();
await say(
  "And the fleet moved",
  "harvester's iteration settled and its next wake was written in the same transaction. A crash " +
    "between those two writes would repeat the iteration or lose the bot; there is no gap to crash in.",
  5600,
);
await glide(420, 1200);
await hide();

// ── 3. one bot ──────────────────────────────────────────────────
await visit("/bots/scout");
await say(
  "One bot",
  "scout: what it is for, how it is scheduled, what it has done, and what it has said.",
  4000,
);
await glide(420, 1300);
await say(
  "Cadence is data",
  "A continuous bot must declare a model-free precondition. Without one, finding out there is " +
    "nothing to do costs a full vendor turn every interval.",
  5200,
);
await glide(900, 1300);
await say(
  "Its iterations",
  "Every run carries a classified outcome — work done, no work, rate limited, blocked, retryable. " +
    "That outcome is what computes the next wake.",
  5000,
);
await glide(1500, 1400);
await say(
  "Its channel",
  "One substrate for human-to-bot and bot-to-bot. Reports, lessons and verdicts all land here, and " +
    "a fresh body is briefed from them — a disposable body is not an amnesiac one.",
  5400,
);
await hide();

// ── 4. bots that make bots ──────────────────────────────────────
await visit("/proposals");
await say(
  "Bots that make bots",
  "A bot may define a bot and request its activation. A human enables it — which makes runaway " +
    "replication structurally impossible rather than merely discouraged.",
  5400,
);
await say(
  "The definition, not the pitch",
  "The rationale is what the bot says it wants. The definition is what it would get, and it is " +
    "shown in full — approving the first without reading the second is the whole attack.",
  5600,
);
await say(
  "And it cannot pay for itself",
  "The child's 40 iterations are carved from scout's remaining, atomically. A bot cannot create " +
    "capacity by creating bots; it can only divide what it already had.",
  5200,
);
await hide();

// ── 5. the boundary ─────────────────────────────────────────────
await page.goto(`${BASE}/bots`);
await mount();
await say(
  "The page is not open",
  "Bots run on this box with network access, so reaching the port cannot be authority — a bot could " +
    "otherwise curl this page and approve its own gate.",
  5400,
);
await say(
  "The token is the lock",
  "It lives in a directory the per-bot sandbox already withholds. The one file that authorises a " +
    "decision is the one file a bot cannot read.",
  5000,
);
await hide();

await visit("/bots");
await say(
  "army bots serve",
  "Served by the control plane itself — no framework, no build step, and no patch to a file " +
    "upstream changes weekly.",
  4600,
);
await page.waitForTimeout(900);

await context.close();
await browser.close();
console.log("recorded");
