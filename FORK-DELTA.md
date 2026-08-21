# Fork delta

Everything this fork carries that upstream does not, why, and what would let it
go away. When this table is empty the contributor-fork model has fully paid off.

The number here is an operational measure of how well upstreaming is going. It
is not a measure of whether the system works, and it does not belong in any
definition of done.

## Carried patches

| Change | Why it is here | Upstream | Retires when |
|---|---|---|---|
| Persist outstanding elicitations | An approval outstanding when the server restarts is lost, while the sidebar badge — which *is* persisted — keeps asserting one is waiting. The loop ends every iteration in an approval, so this is on the critical path. | [#5144](https://github.com/omnigent-ai/omnigent/issues/5144), PR pending | the PR merges |
| Document the Grok Build and Devin harnesses | Both are builtin harnesses with no user-facing mention anywhere, so `--harness grok` is undiscoverable. | [PR #5148](https://github.com/omnigent-ai/omnigent/pull/5148) | the PR merges |

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
