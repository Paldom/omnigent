/**
 * Records the net-fee-researcher bot working on a real repository.
 *
 * Nothing here is staged. The bot is running against `~/git/crypto`
 * (trading-army) through a git worktree on branch `bots/researcher`, its body
 * is a real Codex session, and the finding on screen was produced by that
 * session reading the repository and Kraken's published fee schedule.
 *
 * Usage: node walkthrough.mjs <app-url> <out-dir>
 * Needs PLAYWRIGHT_CHROME (a full Chromium) and PLAYWRIGHT_CORE.
 */
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE || "playwright-core");

const BASE = process.argv[2] || "http://localhost:5199";
const OUT = process.argv[3] || "/tmp/demo/video";

const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROME || undefined,
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
  recordVideo: { dir: OUT, size: { width: 1440, height: 900 } },
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

async function mount() {
  await page.addStyleTag({ content: CAPTION_CSS });
  await page.evaluate(() => {
    if (document.getElementById("wt")) return;
    const el = document.createElement("div");
    el.id = "wt";
    document.body.appendChild(el);
  });
}

async function say(kicker, text, hold = 4000) {
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

await page.goto(`${BASE}/bots`, { waitUntil: "networkidle" });
await page.waitForTimeout(3000);
await mount();

// ── 1. a real fleet on a real repository ────────────────────────
await say(
  "Bot mode · a real project",
  "One bot, against <em>~/git/crypto</em> — an autonomous Freqtrade research system that is " +
    "genuinely running and genuinely stuck: no strategy it has found is profitable net of fees.",
  6000,
);
await say(
  "Its body is an ordinary session",
  "The sidebar's Sessions list is the real Codex sessions this bot opened. A bot owns no process " +
    "between iterations; a run borrows one and gives it back.",
  5600,
);

// ── 2. the finding ──────────────────────────────────────────────
await say(
  "What it found, unattended",
  "The project's README says Kraken costs 0.40% a side — 0.80% round trip. The bot checked the " +
    "published schedule, traced the value through the code, and counted the rows.",
  6200,
);
await say(
  "The README is stale",
  "0.40% is the <em>maker</em> rate. The 75 leaderboard rows charged 0.80% at entry <em>and</em> " +
    "exit — <em>1.60% round trip, twice what the headline says</em>. Every net-edge conclusion in " +
    "that repository rests on this number.",
  7000,
);
await say(
  "It also corrected the question",
  "The queue said 'roughly 150 rows'. The bot said 75 displayed and 41 quarantined, and cited the " +
    "line. A question resting on a false premise gets the premise back.",
  6000,
);

// ── 3. what the verdict is bound to ─────────────────────────────
await say(
  "Bound to what actually happened",
  "branch, commit, <em>files changed: 1</em>, and <em>denied paths touched: none</em> — checked " +
    "against git status after the session ended, outside the model.",
  6000,
);
await say(
  "The list it may not touch",
  "hitl/ · registry/ · tools/deploy_gate/ · policies/ · configs/ · Makefile. The owner's channel, " +
    "the roster, and the only thing that can arm real money. A prompt is a request; this is a check.",
  6400,
);

// ── 4. the workspace ────────────────────────────────────────────
await hide();
await page.getByRole("button", { name: "Runs" }).click();
await page.waitForTimeout(1200);
await mount();
await say(
  "One iteration, one session",
  "Open session goes to the real chat page — the transcript, the tool calls, the files it read. " +
    "Nothing about the harness is hidden behind this view.",
  5600,
);
await hide();
await page.getByRole("button", { name: "Files" }).click();
await page.waitForTimeout(1200);
await mount();
await say(
  "Its charter is a file you edit",
  "charter.md is what the session is briefed with, and queue.md is the questions. Both live in the " +
    "bot's own workspace; the repository only ever receives the report.",
  5800,
);

// ── 5. the human act ────────────────────────────────────────────
await hide();
await page.getByRole("button", { name: "Lineage" }).click();
await page.waitForTimeout(1000);
await mount();
await say(
  "Nothing spawned anything",
  "Depth 0 of 2, no children. This bot was created by a person and cannot create capacity by " +
    "creating bots — the allowance would be carved from its own.",
  5400,
);
await hide();
await page.waitForTimeout(1500);

await context.close();
await browser.close();
console.log("recorded");
