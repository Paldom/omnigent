# Bot mode

Long-running, named, mission-driven bots with their own channel, approval
surface, documentation, workspace and memory — built on the loop in
[`army/`](../../army) rather than beside it.

> **A Bot is data. A body is disposable.**

A bot owns **no process between iterations**. It owns rows. A harness session is
spawned for one run and released at the end of it, so a reboot costs nothing, a
bot waiting on a person holds no seat, and the whole fleet's state is one SQLite
file you can back up.

---

## Five minutes, no vendor

Nothing here needs a subscription, an API key, or a running Omnigent server.

```bash
army bots example > heartbeat.yaml     # a definition that works as written
army bots create heartbeat.yaml --activate
army bots run --once --offline         # tick the loop by hand
army bots status                       # what the fleet is doing
```

Four ticks in, the bot has run an iteration and is waiting on you:

```
heartbeat       waiting_human           —   <- waiting on you
                army approve 7d9944cb3850
```

Answer it, tick once more, and watch it back off:

```bash
army bots pending                      # the question, bound to its iteration
army bots approve <id> --choice continue
army bots run --once --offline
army bots status
#   heartbeat   backing_off            30s
#               nothing to do 1x running
```

That path exercises every load-bearing mechanic — the migration, the atomic
succession, the partial unique index, the outcome policy, the derived status —
and none of the product surface. **If the heartbeat does not loop, nothing else
is worth debugging.**

---

## Writing a bot

A bot is a YAML file. That is the whole point: bots are addable at runtime
because they are data, not code.

```yaml
spec_version: 1
slug: scout                      # its address, its folder, its browser partition
display_name: Scout
title: Research Analyst
persona: >-                      # the standing role, injected into every run
  You read carefully and you do not guess.
mission: >-                      # the objective it pursues
  Watch the inbox for papers worth summarising, and summarise them.
workload: army.workloads.demo:DemoWorkload
workload_config:
  queue_path: ~/bots/scout/queue.txt
  precondition:
    path: ~/bots/scout/queue.txt
harness: claude-native           # optional; the router picks otherwise
wake:
  kind: continuous
  min_interval_s: 60
  precondition: queue_file_has_work
  backoff: {base_s: 60, max_s: 3600, factor: 2}
```

`army bots create scout.yaml` makes it a **draft**. Reading a file is not the
same as deciding to run what is in it, so activation is always a separate move.

### Wake policies

| `kind` | When it runs | Notes |
|---|---|---|
| `rrule` | An iCalendar recurrence, e.g. `FREQ=DAILY;BYHOUR=9` | The phase is pinned at activation, so it does not drift |
| `continuous` | Every `min_interval_s`, backing off when idle | **Must** declare a `precondition` |
| `on_message` | When mail arrives | No schedule at all; an insert wakes it |
| `manual` | Only `army bots wake` | |

**A continuous bot must declare a model-free precondition.** Without one,
finding out there is nothing to do costs a full vendor turn every interval —
ten such bots at an hourly floor burn about 240 turns a day producing nothing.
The tick evaluates the check with no model call; only a true result spawns a
body. Built-ins: `always`, `never`, `queue_file_has_work`, `path_exists`,
`inbox_has_unacked`.

A bot **names** a precondition; it can never define one. Registration happens in
the operator's own process, so a bot invented at 3am by another bot can only
choose from checks a person already wrote.

### Missed wakes coalesce

A daily bot that was off for a week fires **once**, not seven times.

---

## How an iteration ends

Every run reports one of five outcomes, and that is what computes the next wake:

| Outcome | Next wake |
|---|---|
| `WORK_DONE` | streaks reset; wake at the floor |
| `NO_WORK` | idle streak up; back off exponentially with jitter |
| `RATE_LIMITED` | the vendor's whole lane closes, durably |
| `BLOCKED` | **no next wake at all** — an insert wakes it |
| `RETRYABLE_ERROR` | short retry, bounded by the error streak |

A workload may declare only `WORK_DONE` or `NO_WORK`. Everything else describes
something the supervisor observed — a person, a vendor, a failure — and a
workload has no way to know those are true.

### Succession is one transaction

Ending an iteration and scheduling the next one are two writes. A crash between
them either repeats the iteration or loses the bot forever: `next_due_at` stays
`NULL`, `NULL <= now` is false in SQLite, and the scan skips a bot whose work is
finished.

So the terminal transition, the command consume and the successor wake are
**one** SQLite transaction, and there is no second "bump the bot" path to
forget. `test_a_crash_between_the_two_writes_leaves_neither` kills the process
between them and asserts nothing is half-done.

---

## Status is derived, never stored

A bot's own lifecycle is four states and only a human moves it:

```
DRAFT ──activate──► ACTIVE ⇄ PAUSED ──► RETIRED
```

What it is *doing* is nine values computed at read time from the runs, the
vendor gates and the clock — `RUNNING`, `WAITING_HUMAN`, `WAITING_RESOURCE`,
`BACKING_OFF`, `SCHEDULED`, `DUE`, `WAITING_EVENT`, `MANUAL`, `BLOCKED`. None of
them is a column. Two state machines that can disagree is the 3am page, and the
one that goes stale is always the one on the screen.

The question the roster exists to answer at a glance is **which one is stuck?**

---

## Approvals

A verdict is not "yes". A verdict is *yes, to this exact operation, under the
policy in force when it was asked, against the run version it was asked at*.
Anything looser is a permission slip with the amount left blank: the plan
executed after an answer is a fresh plan, and a bare yes authorises whatever
that turns out to be.

A mismatch **re-asks** rather than being honoured. A denial needs no such check
— demanding a fresh question before someone may say no is how a gate becomes
the thing people route around.

**Only a decision creates a Command.** Nothing written when the question is
asked can be mistaken for the answer by a tick that consumes the oldest
unconsumed command as the verdict.

**Expiry never approves.** A question nobody answers pauses the bot and says so.
A system that approves on silence makes a holiday into a blanket authorisation.

### Money-critical verbs do not go through the channel

`spend`, `execute_order` and `add_dependency` are `ALWAYS_OWNER`. A click in a
bot's channel — or on the web page — never satisfies one. They need a one-shot
grant signed on the owner's own path:

```bash
army bots approve <id> --grant '<token>'
```

With no `ARMY_BROKER_KEY` configured, those verbs are **refused outright**.
Refusing is safe; pretending there is a boundary is not.

---

## Channels

One table for human↔bot and bot↔bot, because they are the same problem —
ordered, durable delivery to a named party — and splitting them means two
delivery guarantees to get right.

```bash
army bots channel scout          # ask, verdict, reports, mail
army bots pending                # everything waiting on you
```

The ack is written **after** the receiving bot commits its transition, in the
same transaction. A crash mid-processing therefore redelivers rather than loses.

ACP is deliberately not used for this. It is a transport for driving a harness —
no durable queue, no cursor, no replay — and running it between two bots on one
Mac would be protocol for protocol's sake.

---

## The page

```bash
army bots serve --host 100.x.y.z    # your tailnet address
```

A roster and an approval surface, served by the control-plane process. No
framework, no build step, no new dependency — `http.server` and a string, with
the design tokens read out of Omnigent's own `index.css`.

It is **not** a section in the Omnigent web app, on purpose. Upstream hard-codes
its routes and its navigation, so an integrated page would mean carried patches
in files that change weekly, in a repository that lands about a hundred issues a
week. This costs zero upstream files, forever.

> **There is no authentication.** The answer is the tailnet, not a login form
> nobody would maintain. Bind the tailnet address; never a public one. Anyone
> who can reach the address can answer approvals — except the owner-only verbs,
> which still need a signed grant.

`GET /api/bots` returns the same view as JSON, for a phone shortcut or a status
bar.

---

## Bots that make bots

A bot may **define** a bot and **request** its activation. A human enables it.
That single rule is what makes runaway self-replication structurally impossible
rather than merely discouraged.

The caps behind it, each closing a different hole:

| Guardrail | Rule |
|---|---|
| Depth | ≤ 2. A child may not create grandchildren. |
| Fan-out | ≤ 3 live children per parent. |
| Fleet | ≤ 10 active bots, which is what the box was sized for. |
| **Budget** | Carved from the parent's remaining, atomically. |
| TTL | A child with no reason to exist retires on its own. |
| Cascade | Retiring a parent retires its descendants. |

The budget is the only one that actually bites. **A bot cannot create capacity
by creating bots** — it can only divide what it already had, so a runaway
starves itself rather than starving you. It is also the only backstop against a
*poison mission*: a bot that reports `WORK_DONE` every iteration and never
finishes has no idle streak to back it off.

Budgets are denominated in iterations, not dollars. On subscription auth there
is no dollar signal at all — Omnigent's own budget policies take `max_cost_usd`
and never fire. What is scarce is turns.

---

## What a fresh body is told

A bot throws away its session transcript every iteration. That is the price of
owning no process, and it is worth paying only if something assembles the
continuity back:

1. **Who you are** — persona and mission, from the *pinned revision*, not the
   current row. A run executes the definition it started under.
2. **Where you got to** — the last few outcomes.
3. **What was decided** — recent verdicts. The most expensive thing to forget.
4. **What arrived** — unread mail, with a cursor that makes reading it
   idempotent.

All of it bounded, because an unbounded briefing grows until the context window
truncates it from the top — silently dropping the persona, which is the one part
that must never be dropped.

---

## Isolation, and its honest limits

OpenBot gives every bot a container. That model does not transfer, for one
specific reason: **the vendor CLIs are logged in on the host.** Put the bot in a
container and it has no auth; mount the credentials in and the boundary is
decorative while making theft easier.

| Dimension | Here | Status |
|---|---|---|
| Filesystem | git worktree per bot | real |
| Process | Seatbelt profile, one writable directory | real |
| Vendor configs | excluded from read paths | real, and load-bearing |
| Browser | storage partition per bot | **needs the Electron change** |
| Network | shared | **not isolated** |
| Credentials | host, shared per vendor | forced by subscription auth |

The row people skip is the vendor one. Without it, every bot's shell can read
and invoke every vendor's CLI login — so a cooled lane, a child's carved budget
and the router's vendor choice are all one `codex` subprocess away from being
bypassed. `assert_agent_environment_is_clean` does not catch it, because it
inspects environment variables and a CLI login lives in a config file.

> 🔒 **Authenticated autonomous browsing is refused** until per-bot storage
> partitions are in force. Browser views are keyed per conversation but share
> one cookie jar, so a bot logged into a site would be visible to every other
> bot — and the failure is silent. Unauthenticated browsing stays allowed. Set
> `[bots] browser_partitioned = true` once the Electron side passes a partition.

### What is done, and what is not

`browserViewRegistry` now takes a `resolvePartition(conversationId)` and passes
the answer to the view, so the *mechanism* exists and is tested. It defaults to
today's shared jar.

What does **not** exist yet is the thing that makes it safe: a
**server-authoritative** conversation→bot mapping. The registry is handed a
`conversationId` by the renderer, and a bot may own several conversations in one
iteration, so resolving the partition needs an answer the server gives — not one
the renderer supplies, because a renderer that can name its own partition can
ask for another bot's logged-in session.

Until that mapping is threaded through the API and IPC boundary, the refusal
above is the control that actually holds. Turning `browser_partitioned` on
before then would remove the refusal without providing the isolation.

**Take the wheel** follows OpenBot including the part people get wrong: while a
human is driving, bot actions are **refused, not queued**. A queued action would
run against whatever page the person navigated to.

---

## Operating it

```bash
army bots run --interval 10          # the loop
army bots status                     # the roster, stuck things first
army bots show scout                 # definition, revisions, recent iterations
army bots wake scout                 # make it due now — the recovery for a lost bot
army bots pause|resume|retire scout
```

### When something looks wrong

| Symptom | What it means |
|---|---|
| `blocked`, nothing pending | Its succession was lost. `army bots wake <slug>` restarts it. Logged once, not every tick. |
| `paused` with a reason | The system stopped it. The reason says why; fix it and `resume`. |
| `waiting_resource` | Its vendor lane is cooling. It goes when the lane opens; nothing to do. |
| `backing_off`, growing | It keeps finding nothing. After 12 in a row it pauses and asks for a look. |

The succession write logs what it decided — bot, run, target, outcome, next
wake. That line is the one worth grepping for at 3am.

---

## What Bot mode deliberately does not have

No per-bot daemon. No second run state machine. No containers. No ACP message
bus. No global chat. No workflow DSL. Persona, mission, cadence, budget, docs
reference and roster are all YAML.

The delta is small on purpose, and keeping it small is what keeps the fork
rebaseable — see [`FORK-DELTA.md`](../../FORK-DELTA.md).
