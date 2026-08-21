---
name: iteration-report
description: The shape of the evidence an iteration puts in front of the owner. Load before ending an iteration or before reporting back as a worker.
---

# Iteration report

The owner reads this at 7am on a phone, having not seen the last six hours.
Assume no context and no patience. Lead with what you want, then justify it.

## As a worker, reporting back

Four things, in this order:

**What I did.** One paragraph. Files at `path:line`, not "several files".

**How I verified it.** The exact commands and their results. `pytest tests/foo
-q → 41 passed` is evidence. "Tests pass" is a claim. If you did not run
something you would normally run, say which and why.

**What I did not do.** Anything in the task you left, anything you noticed and
deliberately did not touch, anything you were unsure about. This section being
empty is itself a claim, so only leave it empty when it is true.

**What I would want a reviewer to look at.** The part you are least sure of.

## As a reviewer

You did not write it, and that is the whole reason you were asked. Judge it
against the acceptance contract, not against the version you would have
written.

Separate three things and do not blur them:

- **Blocking** — this is wrong, or it breaks something. Give `file:line` and
  say what fails.
- **Non-blocking** — this is a real problem but it can ship and be fixed.
- **Suggestion** — you would have done it differently. Say so once, then let it
  go.

A review with no blocking issues is a fine review. Inventing one to look
thorough wastes an iteration.

## As marshal, ending an iteration

The approval carries:

- **The recommendation, first.** Merge, iterate, discard, or stop — and one
  sentence on why.
- **What was attempted**, in the owner's terms, not the agents'.
- **The gate results.** Actual commands and actual output.
- **The cross-vendor review**, including who reviewed whom. A diff reviewed by
  the vendor that wrote it is not a cross-vendor review, and if that is what
  happened, say so rather than presenting it as one.
- **What it cost** — which vendors, how many sessions, roughly how long.
- **What is still unknown.** The thing you would check next if you had another
  hour.

Then stop. The gates say what the evidence is. They do not say what to do about
it, and an iteration that answers its own question has skipped the only step
that was mandatory.
