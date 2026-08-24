# The librarian's queue

One question per line. The librarian claims the first unclaimed one each night,
marks it `taken:` in place before it starts, and writes what it found to
`reports/`.

An empty queue is a normal night, not a failure — the bot reports no work, the
backoff widens, and nothing wakes anybody up. Adding work to this crew is a
text edit in this file, not a deployment.

Lines starting with `#`, `taken:` or `done:` are skipped, so this paragraph is
safe and so is your own commentary.

Read every report in `~/bots/*/reports/` from the last 14 days. Is there anything true only across two of them — a change in one page that makes a change in another page matter more, or contradict it? If nothing connects, say so plainly and stop.

Which pages has the crew reported UNCHANGED for more than a month? For each, say whether it is genuinely stable or whether the question it was given is too vague to ever change — a watcher that cannot fail is a watcher that is not watching.

Build or refresh `index.md`: one line per report, newest first, with the date, which bot filed it, and the one sentence that would make somebody open it.
