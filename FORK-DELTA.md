# Fork delta

Everything this fork carries that upstream does not, why, and what would let it
go away. When this table is empty the contributor-fork model has fully paid off.

The number here is an operational measure of how well upstreaming is going. It
is not a measure of whether the system works, and it does not belong in any
definition of done.

## Carried patches

| Change | Why it is here | Upstream | Retires when |
|---|---|---|---|
| Persist outstanding elicitations | An approval outstanding when the server restarts is lost, while the sidebar badge — which *is* persisted — keeps asserting one is waiting. The loop ends every iteration in an approval, so this is on the critical path. | [#5144](https://github.com/omnigent-ai/omnigent/issues/5144) · [PR #5151](https://github.com/omnigent-ai/omnigent/pull/5151) | the PR merges |
| Document the Grok Build and Devin harnesses | Both are builtin harnesses with no user-facing mention anywhere, so `--harness grok` is undiscoverable. | [PR #5148](https://github.com/omnigent-ai/omnigent/pull/5148) | the PR merges |
| Skills / hooks / MCP / plan-mode capability axes | Nothing in the capability model can say whether a harness can load a skill, so the answer lives only in the runner's relay code. The roster needs it to route work away from harnesses that cannot do the job. | [#5152](https://github.com/omnigent-ai/omnigent/issues/5152) · [PR #5153](https://github.com/omnigent-ai/omnigent/pull/5153) | the PR merges |
| Relay the skill tools to native harnesses | `load_skill` is what discovers `~/.agents/skills` and friends, and it was absent from the native relay — the only tool surface a native session sees. The roster runs native harnesses, so without this a shared skill reaches half the fleet. | [#5105](https://github.com/omnigent-ai/omnigent/issues/5105) · [PR #5184](https://github.com/omnigent-ai/omnigent/pull/5184) | the PR merges |
| Stop the ToolManager suite reading host skills | Five tests fail on a fresh clone for anyone with skills in `~/.claude/skills` — which is anyone running this project. Carried so the fork's own suite is trustworthy. | [PR #5186](https://github.com/omnigent-ai/omnigent/pull/5186) | the PR merges |
| Per-conversation browser storage partitions | Browser views are keyed by conversation but share one cookie jar, so an agent that logs into a site is logged in for every other conversation too — silently, since nothing errors and the wrong session is simply used. One optional injected resolver on `browserViewRegistry`, defaulting to today's behaviour. | not yet filed | the seam is upstreamed, or a `partition` lands upstream by another route |
| Bench `--live` for harnesses that need no gateway | The capability bench refused to run at all without a metered gateway, including for the eight natives that log their own model in. FND-04 needs it and D6 forbids the gateway. | [#5187](https://github.com/omnigent-ai/omnigent/issues/5187) · [PR #5188](https://github.com/omnigent-ai/omnigent/pull/5188) | the PR merges |
| Bots route in `web/src/App.tsx` | Routes are a hard-coded list; a section cannot be registered from outside it. Two lines: a `withPageView`+`lazy` binding and one `<Route>`. | not filed — a route registry is a large upstream ask | upstream grows a route/nav extension point |
| Bots row in `web/src/shell/Sidebar.tsx` | Navigation is likewise a hard-coded list, and the active-item hook is a chain of path tests. Adds an entry, a branch in `useActiveNavItem`, and the badge count. | same | same |
| Bots router in `omnigent/server/app.py` | Two lines in the router block, so the page has a same-origin endpoint. The router itself is an added file that imports nothing from `army`. | same | same |

## Not patches — additions alongside

These add files without changing upstream behaviour, so they cost nothing at
rebase and are not counted as delta:

| Addition | What it is |
|---|---|
| `army/` | The durable workflow control plane. Deliberately *over* Omnigent's session API rather than inside it, so exactly one component owns durable workflow state and upstream's own direction is not fought. |
| `army/bots/` | Bot mode: long-running, mission-driven bots over that loop. Imports `army`, and nothing in `army` imports it — `tests/army/bots/test_boundary.py` parses the core modules to enforce that, so deleting the directory leaves a working `army`. The one crossing is a lazy import inside `army/cli.py`'s `bots` subcommand. |
| `omnigent/server/routes/bots.py` | The `/v1/bots` proxy. Imports nothing from `army`; one HTTP call to a loopback port, which is the boundary the CLI already uses. |
| `web/src/pages/BotsPage.tsx`, `lib/botsApi.ts`, `hooks/useBots.ts` | The section itself. New files, so only the three list entries above are carried. |
| `docs/agent-army/bots.md` | How to run it. |
| `tests/army/bots/`, `tests/server/routes/test_bots.py`, `web/src/pages/BotsPage.test.tsx` | Durability, scheduling, delivery, capability and UI tests for the above. |
| `agents/marshal/` | The orchestrator and its six-vendor roster. Agent YAML only — no code, so nothing here can break on a rebase. |
| `docs/agent-army/` | Setup and operation. |
| `tests/army/` | Durability and bypass tests for the above. |

## Rebase notes

- `omnigent/db/db_models.py` is touched by most schema PRs upstream. Rebase
  often; a stale branch conflicts there before anywhere else.
- The elicitation patch touches the publish chokepoint
  (`omnigent/runtime/session_stream.py` → `pending_elicitations.record_publish`)
  and the session-snapshot helper. Both are stable, but check them after any
  upstream change to the elicitation path.
- Measure the cost. If a monthly rebase starts taking more than a day, the
  economics of carrying this privately have changed and the answer is to push
  harder on upstreaming, not to carry more.
- `scripts/sync-upstream.sh` does the fetch, the merge and the re-checks, and
  tells you which rows above upstream has since merged. Run it often — upstream
  lands roughly a hundred issues a week, so a fork that syncs monthly conflicts
  monthly.

## Why Bot mode's UI is a patch after all

This section previously argued the opposite, and the argument was wrong in the
way rebase-cost arguments usually are: it optimised the metric above instead of
the product. Bot mode's UI is a section in the Omnigent app, `/bots`, and the
three rows it costs are in the table.

The rebase risk is real but smaller than it looked. All three patches are
*additions to lists* — one route, one nav entry, one `include_router` — not
edits to logic. A conflict there is resolved by re-adding the line, which is
the cheapest conflict class there is. Everything with substance lives in files
upstream does not have: `web/src/pages/BotsPage.tsx`, `web/src/lib/botsApi.ts`,
`web/src/hooks/useBots.ts`, `omnigent/server/routes/bots.py`.

What is bought is worth more than three list entries: one place to look, the
app's own session and shortcuts, and no second URL to remember. The badge on
the nav row is the point — an operator learns a bot is waiting on them while
doing something else, which a separate page can never do.

The server route is a **proxy**, deliberately. It imports nothing from `army`;
it forwards to the control plane on loopback and holds the token there, so the
browser never sees a secret and a verdict is still checked exactly once, in the
place that owns the bindings. Delete `army/` and the route answers "not
running" — which is also what it answers on a machine that has never run a bot,
and what the page renders as a sentence rather than an error.

`army bots serve` stays. It is the process the proxy talks to, it is what
answers on a phone over Tailscale with no Omnigent server running, and it is
the surface `tests/army/bots/test_web.py` covers.
