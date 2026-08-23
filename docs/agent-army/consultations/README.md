# Consultations

Three consultations that shaped this branch, verbatim and attributed. They are
kept because the commit messages cite them, and because most of what is
load-bearing in Bot mode is here as an argument rather than in the code as a
comment.

Each ran the same question past four or five flagship models at maximum
reasoning effort — Claude Fable 5, GPT-5.6 Sol, Grok 4.6, Kimi K3, and
Gemini 3.7 Flash where it had quota.

| | What was asked | What came of it |
|---|---|---|
| [`advise-01-architecture.md`](./advise-01-architecture.md) | Where should Bot mode live so upstream's churn never touches it? How do you get multi-statement transactions out of a store that opens a connection per method? | Settled `army/bots/` as a subpackage with a one-way import, the `conn=` passthrough, column-detection migration, and composition over the supervisor. Four of four agreed on the boundary; three of four on the migration. |
| [`advise-02-review.md`](./advise-02-review.md) | Adversarial review of the scheduler. Find the interleaving that loses work. | Sixteen real defects, including the deferred/immediate deadlock, `READY` settling a live run, and "daily at nine" drifting by one runtime a day. |
| [`advise-03-security.md`](./advise-03-security.md) | Attack the approval path as a prompt-injected bot with a shell. | The one that mattered: the page had no authentication and bots have network access, so a bot could `curl` its own approval. Everything else sat behind that. |

## How to read them

They disagree, sometimes sharply, and the disagreements are the useful part.
Where three said column detection and one said `user_version`, the argument for
`user_version` contained the reason not to use it. Where two flagged
proposal-time code execution, both marked it conditional on a file they could
not see — and it was a false positive.

Two habits made these worth the quota:

- **Ask for the failure, not the opinion.** "Find the exact interleaving and
  the observable symptom" produced findings; "review this" would have produced
  praise.
- **Ask what you will get wrong.** The last question in the architecture
  consultation was "what is the specific mistake you predict I will make in the
  first 2000 lines?" Fable answered "you will hold a transaction across an HTTP
  call", and the nested-transaction deadlock that later cost an afternoon was
  the same mistake in a different coat.

## What they did not catch

Worth recording, since a review's blind spots are as informative as its hits.
Nothing in three consultations found the argparse trap in `army bots --config`,
the run-version binding that made every verdict stale at birth, or the
classification bug that counted "asked a question" as "found nothing to do".

All three were found by running the thing.
