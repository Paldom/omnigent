# nightshift

A standing crew for the things nobody remembers to check.

https://github.com/Paldom/omnigent/raw/feat/bot-mode/examples/nightshift/media/bot-mode.mp4

*70 seconds: the roster, a bot's question, the browser it drove, taking the
wheel, and acknowledgement that isn't permission. Filmed against this example —
`walkthrough.yaml` is the scenario, and nothing in it is a fixture.*

Every other example here is a **session**: you ask, it works, it answers, it
ends. This one has no beginning. The crew was activated some week in the past
and it is still going — waking on a schedule, reading pages it has read before,
saying nothing when nothing has moved, and asking you a question on the morning
something has.

## The work this is for

Some jobs are not hard. They are *only* hard because they have to happen again.

- A Python version drops out of security support. Nobody had it in a calendar,
  and the first anyone hears is a red build on an unrelated pull request.
- A dependency relicenses. The terms change, a release ships under them, and
  the people who needed to know find out from a blog post, months later, with
  the new terms already in production.
- A vendor has an incident at 03:00 and it is closed by the time you look.

None of these is a research problem. Each is one careful reading, repeated for
two years, by something that never gets bored and never decides this week
probably doesn't matter. That is not a thing a session can be. A session with a
cron in front of it is a session that starts from nothing every time — it has
no memory of what the page said last week, so it cannot tell you the one fact
that matters, which is *that it changed*.

A bot has that memory, because a bot is not a process. It is rows: its charter,
its schedule, its budget, every question it has asked and every answer it was
given. Kill the loop mid-iteration and nothing is lost — the run is still in
`COLLECTING`, and the next tick collects it. **A bot is data; a body is
disposable.**

## The crew

| Bot | Watches | Wakes | Why a person doesn't |
|---|---|---|---|
| `python-eol` | the published Python support window | weekly | The date moves once a year, on a schedule nobody has |
| `licence-watch` | a dependency's licence terms | weekly | Nobody schedules "re-read a licence I already read" |
| `status-watch` | a vendor status page | daily | It is usually fine, which is exactly why it goes unchecked |
| `librarian` | *what the other three wrote* | nightly | The finding is in the join, and no watcher can see it |

The librarian is the interesting one. Three reports are individually
unremarkable; the thing worth knowing is that the licence changed in the same
week the version shipping under it went end of life. Neither watcher can see
that, because neither is looking at the other's page.

That is cooperation without a conversation: one bot leaves a durable artifact,
another reads it on its own schedule. It still works when a watcher is asleep,
paused, or was retired last month — which is the property that makes a crew
different from a group chat of agents.

## Run it

Needs a running Omnigent server and one configured harness. Nothing else — no
vendor account for the crew, no API key of its own, no host id to paste.

```bash
# 1. the server the bodies run in
omnigent server &

# 2. enlist the crew
for bot in examples/nightshift/bots/*.yaml; do
  army bots create "$bot" --config examples/nightshift/army.toml --activate
done

# 3. drive it
army bots run --interval 60 --config examples/nightshift/army.toml
```

Then open `/bots` in the Omnigent UI, or the standalone page:

```bash
army bots serve --config examples/nightshift/army.toml
```

To see one work without waiting for its schedule:

```bash
army bots wake status-watch --config examples/nightshift/army.toml
```

## What you will see it do

**Drive a real browser.** Not fetch a URL — drive the page. `browser_snapshot`
names every clickable thing as `[ref=N]`, so the bot opens the collapsed table
the number is actually in. The Screen panel shows that page live, because it is
the same Chromium: one page, two viewers.

**Have its own identity.** Each bot gets its own browser profile, cookies and
all. One bot's compromise is not every bot's session, and the audit trail can
say which of them did a thing.

**Hand you the wheel.** Click *Take the wheel* and the bot's browser actions are
**refused, not queued** — including reads, because a snapshot taken while
somebody is typing a password transcribes it. Hand it back and it carries on,
on the page you left it on.

**Ask for you rather than around you.** A bot that meets a sign-in page has two
options worth having: stop, or ask a person to sign in *on this browser*. It
gets the second. It is forbidden from typing a credential — and forbidden from
going to look for one, which is the interesting half — and the executor refuses
to type into a password or one-time-code field regardless of what the page
argues. The instruction is backed by a mechanism, not by hope.

**Tell you where you already are.** Set `[egress]` in `army.toml` and a bot
parking on a question posts to one webhook with a link. The link answers that
one question, expires, and lands on a confirm page — never a one-click approve,
because chat clients fetch every URL they unfurl and a preview crawler would
click it from inside your network. Nothing inbound: the half of a chat
integration that rots is bot users and event subscriptions, so it is not built.

**Decline to ask.** The commonest outcome is `UNCHANGED`, which costs a backoff
rather than your attention, and widens the interval on its own. A crew that
reports every morning is a crew nobody reads by Thursday — and then the silence
stops meaning anything, which is worse than no crew at all.

**Take an acknowledgement that isn't permission.** 👀 💡 ❓ ⚠️ under a report
reach the bot in its next brief, with the boundary in the same breath. There is
deliberately no tick: beside a pending question a tick reads as *approved*, and
an approval here binds a verdict to an action hash, a policy version and a run
version. Nothing that does not may stand in for it.

## What this example taught the code

Four bugs, all found by running it on a machine that had never run it, and by
filming it.

A bot definition used to need a **host id** — a uuid only valid on the box it
was copied from — or its sessions got no runner and failed with
`runner_failed_to_start`, which reads like a broken harness rather than a
missing field. The client fills it in now.

A bot's **workspace** had to already exist, or the session create failed with
"workspace path does not exist on host", naming a host the operator never
mentioned. The watcher creates the directory it was told to work in.

**Taking the wheel navigated you off the bot you had just taken it from.** The
page cleared its selection after every successful action — right for a verdict,
which removes the row, wrong for the wheel, which decides nothing. Found while
recording the walkthrough, because the button stubbornly kept the wrong label.

And the librarian found one in this example's own files: `research-queue.md`
shipped with an **uncommented prose header**, so the queue handed it the
sentence "One question per line." as that night's work. It spent the iteration
reporting that its brief was not a question — correct behaviour on malformed
input, and a better bug report than most.

An example is worth having partly because it is the first honest user.

## What it is not

It is not a scraper, and it is not a monitor. It reads a handful of pages
carefully and tells a person when the answer changed. If you need a hundred
pages a minute, write a scraper — this costs an agent turn per look and is
priced for the pages where being wrong is expensive.

The Screen panel is not a network monitor either: a bot's harness can still
fetch a URL with its own tooling, and nothing there sees that. The UI says so
rather than implying otherwise.
