# Bot mode — what is built, and what is not

The backlog is [`plan-2/04-backlog.md`](https://github.com/Paldom/omnigent/blob/feat/bot-mode/docs/agent-army/bots.md)
in the design repository: 32 tickets across five milestones. This is the honest
status of each, so nobody has to read the diff to find out what is missing.

The rule applied throughout: **a stub of a safety control is worse than its
absence.** Where something is not built, it is refused rather than faked.

---

## Built

### M1 — bots exist and loop

| | |
|---|---|
| BOT-01 | `bots` + `bot_revisions`. A definition edit makes rev 2 and leaves rev 1 intact; a run pins the revision it started under. |
| BOT-02 | `runs.bot_id` / `revision_id` / `outcome`, and the partial unique index. `paused` stays *inside* it, so pausing a run stops the bot. |
| BOT-03 | Five structured outcomes, and the wake each one computes. |
| BOT-04 | The wake scan. Missed ticks coalesce — a daily bot down for a week fires once. |
| BOT-05 | Model-free preconditions, mandatory for continuous bots. A bot names one; it can never define one. |
| BOT-06 | Nine derived statuses, none of them stored. |
| BOT-24 | Atomic succession, with a kill-the-process test. |
| BOT-25 | `provider_gates`, so a reboot does not make every bot on a limited vendor instantly due. |
| BOT-26 | Workload registry, resolved from the pinned revision. |
| BOT-27 | The stalled scan — once per state change, not once per tick. |
| BOT-28 | Column detection. Idempotent on a fresh database, a live one, and one that already ran Bot mode. |

### M2 — channels and HITL

| | |
|---|---|
| BOT-07 | `messages` + `message_deliveries`. Ack takes the caller's transaction, so a crash redelivers. |
| BOT-09 | Per-bot channel, one thread per run or approval. |
| BOT-10 / BOT-29 | `approval_requests`, binding action hash + policy version + run version. Only a decision creates a Command. |
| BOT-12 | Expiry pauses and reports. Never auto-approves. |
| BOT-30 | The Broker is **constructed**. Owner-only verbs need a signed one-shot grant, or are refused. |
| BOT-31 | The free-text authorisation path is removed for bots. The words are still captured. |
| BOT-32 | Continuity assembly — persona from the pinned revision, recent outcomes, recent verdicts, unread mail, all bounded. |

### M3 — per-bot computer

| | |
|---|---|
| BOT-14 | Authenticated autonomous browsing refused. This is the control that currently holds. |
| BOT-15 | `~/bots/<slug>/` — worktree, charter, runbook, reports. `bot_docs` indexes it. |
| BOT-16 | Per-bot sandbox profile, with other vendors' CLI configs excluded from read paths. |
| BOT-17 | Take the wheel. While a person drives, bot actions are refused, not queued. |

### M4 — bots that make bots

| | |
|---|---|
| BOT-19 | `spawn_requests`. Propose, don't activate; an adopted child starts in `DRAFT`. |
| BOT-20 | Depth ≤ 2, fan-out ≤ 3, fleet ≤ 10, TTL, cascade — and the budget carved from the parent, which is the one that bites. |

### M5 — surface

| | |
|---|---|
| BOT-21 | `army bots serve`, plus the CLI. Served by the control plane, so no upstream patch. |
| BOT-22 | The YAML format, versioned, refusing rather than defaulting. |
| BOT-23 | Reports cite the run that produced them and land in the bot's docs repo. |

---

## Partly built, and deliberately so

### BOT-13 — browser storage partitions

The **seam** exists: `createBrowserViewRegistry` takes a
`resolvePartition(conversationId)` and passes the answer to the view, defaulting
to today's shared jar. It is tested and upstreamable.

What is missing is what makes it *safe*: a server-authoritative
conversation→bot mapping. The registry is handed an id by the renderer, and a
renderer that can name its own partition can request another bot's logged-in
session. A bot may also own several conversations in one iteration, so the
mapping is not one-to-one.

Until that is threaded through the API and IPC boundary, BOT-14's refusal is the
control. **Do not set `browser_partitioned = true` before then** — it removes
the refusal without providing the isolation.

---

## Not built

### BOT-08 — `sys_read_inbox` over durable rows

`sys_read_inbox` is Omnigent's own tool for draining async sub-agent
completions, woven into the async-work topic system and the auto-collect drain.
Rewriting it to read `message_deliveries` would make `omnigent/` depend on
`army/bots/` — inverting the dependency direction the whole branch is built on,
and converting an addition-alongside into a deep carried patch in files upstream
changes weekly.

The backlog buckets it 🟢 **UPSTREAM** for exactly this reason. It fixes a real
upstream bug ([#3274](https://github.com/omnigent-ai/omnigent/issues/3274) — a
child's result rejected forever, parent idle indefinitely) and is worth filing
on its own merits, as its own pull request, against upstream.

**Bot↔bot messaging is not blocked by this.** It goes through `MessageStore`,
which is what the design specifies. What is unchanged is Omnigent's in-memory
inbox for non-bot sessions.

### BOT-11 — ask-by-failing (the #765 workaround)

Only `TOOL_RESULT`, `OUTPUT` and agent-start collapse a policy ASK to DENY;
`TOOL_CALL` ASK raises a real elicitation. So this is a workaround for a narrow
set of phases and explicitly not the default path.

The design lists what must exist first: *"a checkpoint-before-effect contract
for every pre-ASK effect, and the fault test — effect succeeds → ASK → process
dies → approval arrives → replay does not repeat the effect."* Without that,
non-gated effects from the partial turn double-execute, because `begin_effect`
dedupes only when the second turn re-derives the same idempotency key and native
Bash / Write / `gh` never touch that journal at all.

A half-built version of this is a gate that appears to hold and does not. It is
absent rather than approximated.

### BOT-18 — camoufox profile per bot

**Blocked on an owner decision, not on effort.** camoufox is a new dependency,
and `add_dependency` is one of the three `ALWAYS_OWNER` verbs this system exists
to gate. Adding it unilaterally while building the machinery that forbids doing
so unilaterally would be the wrong thing twice.

It also depends on BOT-13's mapping, since the partition key is what selects the
profile directory.

---

## Known limits, stated rather than hidden

- **Network is not isolated.** Bots share the host's network namespace.
- **Vendor credentials are shared per vendor**, forced by subscription auth. The
  sandbox profile excludes other vendors' config directories from read paths,
  which is what turns the vendor lane from an accounting convention into a
  boundary — but a bot on the Claude lane can still use the Claude login.
- **Bots on one vendor share that vendor's own memory** (`~/.claude`,
  `~/.codex`), so working memory bleeds between them even though Omnigent
  sessions do not.
- **The page has no authentication.** The answer is the tailnet. Owner-only
  verbs still need a signed grant, so the blast radius of a second person on
  that tailnet is "can answer ordinary iteration gates".
- **`runs` has no retention policy.** One row per iteration, indexed by bot, but
  nothing prunes it. Fine for months; not forever.
- **rrule times are UTC.** `BYHOUR=9` means 09:00 UTC, which is not what an
  operator in another timezone will read it as.
