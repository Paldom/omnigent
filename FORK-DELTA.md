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
| Bench `--live` for harnesses that need no gateway | The capability bench refused to run at all without a metered gateway, including for the eight natives that log their own model in. FND-04 needs it and D6 forbids the gateway. | [#5187](https://github.com/omnigent-ai/omnigent/issues/5187) · [PR #5188](https://github.com/omnigent-ai/omnigent/pull/5188) | the PR merges |

## Not patches — additions alongside

These add files without changing upstream behaviour, so they cost nothing at
rebase and are not counted as delta:

| Addition | What it is |
|---|---|
| `army/` | The durable workflow control plane. Deliberately *over* Omnigent's session API rather than inside it, so exactly one component owns durable workflow state and upstream's own direction is not fought. |
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
