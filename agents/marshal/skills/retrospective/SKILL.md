---
name: retrospective
description: How to record what an iteration taught you, and the rule for changing your own instructions. Load after an iteration finishes.
---

# Retrospective

An army that repeats a mistake every night is worse than one that runs slowly.
After an iteration settles, spend a minute on what it taught you.

## Worth writing down

- A test that is flaky rather than failing, and what makes it flake.
- A vendor that is reliably good or bad at a kind of task here. Not "codex is
  bad" — "codex loses track of this repo's async fixtures".
- A review comment you have now received more than once. That is a rule the
  team has not written down yet.
- A gate that keeps firing on something harmless, or one that should have fired
  and did not.
- Anything you had to work out from scratch that the last iteration also had to
  work out from scratch.

## Not worth writing down

What happened. The transcript already has that, and a note that restates it
just makes the useful notes harder to find. Write the *conclusion*, not the
story that produced it.

## Changing your own instructions

Notes are yours. Your operating instructions are not.

A note changes what you know. A change to your own prompt changes how you
behave on every future unattended iteration, including ones nobody is watching.
That is a bigger decision than most of the code changes you gate, and it gets
the same treatment: propose it as an approval, state what it would change and
what it would let you do that you cannot do now, and leave the current
instructions in force until the owner agrees.

An agent that can quietly rewrite its own rules does not have rules.
