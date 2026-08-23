/**
 * Records a walkthrough of the Bots section, inside the Omnigent app.
 *
 * Everything on screen is real: a live Omnigent server, a live control plane
 * in its own process, a real SQLite file, and approvals answered through the
 * same bound path the CLI uses. The only addition is the caption strip,
 * injected as an overlay so the narration sits beside the product rather than
 * replacing it.
 *
 * Usage:
 *   node walkthrough.mjs <app-url> <out-dir>
 *
 * `PLAYWRIGHT_CHROME` must point at a full Chromium — the headless shell
 * cannot record video. `ARMY_CONFIG` points at the control plane's config, so
 * the recording can drive a real tick from a second process mid-take.
 */
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";

// playwright-core is not a dependency of this repo; the recording is a
// developer tool, not part of the build. Resolve whatever is installed.
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

/** Put the caption strip on the page. Survives client-side routing. */
async function mount() {
  await page.addStyleTag({ content: CAPTION_CSS });
  await page.evaluate(() => {
    if (document.getElementById("wt")) return;
    const el = document.createElement("div");
    el.id = "wt";
    document.body.appendChild(el);
  });
}

/** Show a caption and hold it. */
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

/** Click a roster row by its visible name, and let the columns settle. */
async function select(name) {
  await page.getByRole("button", { name }).first().click();
  await page.waitForTimeout(1100);
}

/** A real tick, from a second process, against the same file the page reads. */
function tick() {
  if (!process.env.ARMY_CONFIG) return;
  execFileSync(
    "uv",
    // prettier-ignore
    ["run", "--frozen", "python", "-m", "army", "bots", "run", "--once", "--offline",
      "--config", process.env.ARMY_CONFIG],
    { stdio: "ignore" },
  );
}

// ── 1. the section ──────────────────────────────────────────────
await page.goto(`${BASE}/bots`, { waitUntil: "networkidle" });
await page.waitForTimeout(2500);
await mount();

await say(
  "Omnigent · Bot mode",
  "Long-running bots that own no process between iterations. A bot is data; a body is disposable.",
  4200,
);
await say(
  "A section, not a second app",
  "The app's own sidebar, its own session, its own shortcuts — and a count on the nav row, which is " +
    "how you find out a bot is waiting on you while you are doing something else.",
  5000,
);
await say(
  "The roster",
  "Nine bots in nine operational statuses, none of them stored. Every one is computed from the runs, " +
    "the vendor gates and the clock, so it cannot disagree with what is actually happening.",
  5400,
);
await say(
  "Ordered by consequence",
  "A signature moves money and cannot be taken back. A question only gates an iteration. A proposal " +
    "has created nothing yet. That is the order.",
  4600,
);

// ── 2. the owner signature ──────────────────────────────────────
await say(
  "Owner approvals",
  "<em>spend</em>, <em>execute_order</em> and <em>add_dependency</em> are ALWAYS_OWNER. treasurer " +
    "asked for forty dollars and its own channel cannot answer — the card there has no buttons at all.",
  5600,
);
await say(
  "Bound, not merely asked",
  "The digest is what the grant is signed over, so it is on the card. A signature over a fingerprint " +
    "you were never shown is a signature over a blank.",
  5200,
);
await hide();
await page.getByRole("checkbox").click();
await page.waitForTimeout(900);
await mount();
await say(
  "The affirmation is the signature",
  "The button is disabled until you say you are the owner and that you authorise <em>this</em> " +
    "operation. What guards this is not the checkbox — it is that the signing key is in an " +
    "environment a bot's sandbox does not get.",
  6000,
);
await hide();
await page.getByRole("button", { name: "Sign and release" }).click();
await page.waitForTimeout(2200);
await mount();
await say(
  "Minted, spent, gone",
  "One grant, bound by HMAC to that digest, recorded in <em>used_grants</em> in the same transaction " +
    "as the verdict. The same approval cannot pay twice.",
  5200,
);
await hide();

// ── 3. an ordinary question ─────────────────────────────────────
await select(/^harvester/);
await mount();
await say(
  "An approval is a row",
  "harvester finished an iteration and is waiting. The run holds no session, no process and no " +
    "vendor seat while it waits — a reboot here costs nothing.",
  5000,
);
await say(
  "What the verdict is bound to",
  "The action hash, the policy version, the run version. A verdict that no longer matches its " +
    "question re-asks rather than being honoured.",
  5000,
);
await hide();
await page.getByRole("button", { name: "continue" }).first().click();
await page.waitForTimeout(1600);
await mount();
await say(
  "Recorded, not applied",
  "Only a decision creates a Command, and recording it is all this does. The loop applies it on its " +
    "next tick — so answering while nothing is running is normal.",
  5000,
);
await say(
  "Meanwhile, the loop",
  "<em>army bots run --once</em> — a different process, the same SQLite file. The page is reading " +
    "while the supervisor writes.",
  3600,
);
tick();
await page.waitForTimeout(6000);
await say(
  "And the fleet moved",
  "The iteration settled and the next wake was written in the same transaction. A crash between " +
    "those two writes would repeat the iteration or lose the bot; there is no gap to crash in.",
  5600,
);
await hide();

// ── 4. one bot ──────────────────────────────────────────────────
await select(/^scout/);
await mount();
await say(
  "One bot",
  "scout: what it is for, what it has done, and what it has said. Its channel is one substrate for " +
    "human-to-bot and bot-to-bot alike.",
  5000,
);
await say(
  "A disposable body is not an amnesiac one",
  "Reports, lessons and verdicts all land here, and a fresh body is briefed from them.",
  4600,
);
await hide();
await page.getByRole("button", { name: "Runs" }).click();
await page.waitForTimeout(1000);
await mount();
await say(
  "Its iterations",
  "Every run carries a classified outcome — work done, no work, rate limited, blocked, retryable. " +
    "That outcome is what computes the next wake.",
  5200,
);
await hide();
await page.getByRole("button", { name: "Setup" }).click();
await page.waitForTimeout(1000);
await mount();
await say(
  "Cadence is data",
  "A continuous bot must declare a model-free precondition. Without one, finding out there is " +
    "nothing to do costs a full vendor turn every interval.",
  5200,
);
await hide();

// ── 5. bots that make bots ──────────────────────────────────────
await select(/prospector-child/);
await mount();
await say(
  "Bots that make bots",
  "A bot may define a bot and request its activation. A human enables it — which makes runaway " +
    "replication structurally impossible rather than merely discouraged.",
  5400,
);
await say(
  "The definition, not the pitch",
  "The rationale is what the bot says it wants. The definition is what it would get, and it is shown " +
    "in full — approving the first without reading the second is the whole attack.",
  5600,
);
await say(
  "Two decisions, not one",
  "Activate switches it on. Keep as draft creates it dormant. Saying a bot should exist is a " +
    "separate act from starting it, so approving several in a row has not started several.",
  5400,
);
await say(
  "And it cannot pay for itself",
  "The child's forty iterations are carved from scout's remaining, atomically. A bot cannot create " +
    "capacity by creating bots; it can only divide what it already had.",
  5200,
);
await hide();
await page.getByRole("button", { name: "Activate" }).click();
await page.waitForTimeout(2400);
await mount();
await say(
  "The fleet grew, by a human's hand",
  "prospector-child is active with its own budget, its own workspace and its own browser partition.",
  4800,
);
await hide();
await page.waitForTimeout(1200);

await context.close();
await browser.close();
console.log("recorded");
