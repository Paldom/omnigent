# Bot mode against a real project

`crypto-bot.mp4` is one bot working on `~/git/crypto` — "trading-army", an
autonomous Freqtrade research system that is genuinely running and genuinely
stuck: nothing it has found is profitable net of Kraken's fees. Nothing in the
recording is staged. The body is a real Codex session, the branch and commit on
the approval card are real, and the finding is the session's own.

## What it found on its first iteration

The project's README says Kraken costs **0.40% a side, ~0.80% round trip**. The
bot checked the published fee schedule, traced the value from
`tools/venue/fidelity.py` through the battery command and the leaderboard
ingester, and counted the rows:

> 0.40% is the **maker** rate. The 75 leaderboard rows charged 0.80% at entry
> *and* exit — **1.60% round trip**, twice what the headline says.

Every net-edge conclusion in that repository rests on that number. It also
corrected the question it was given ("roughly 150 rows" → 75 displayed, 41
quarantined, with the line cited) and stated its own evidence gap: the raw
artifact ZIPs are not in the worktree, so it could not reopen each one.

That is the honest shape of the result. The first night was never going to find
a viable strategy; it found a stale premise underneath every attempt to.

## The setup

```bash
# 1. its own control plane, separate from any other fleet
mkdir -p ~/.omnigent/army-crypto
printf '[army]\nstate_path = "%s/.omnigent/army-crypto/army.db"\n' "$HOME" \
  > ~/.omnigent/army-crypto/army.toml

# 2. its own checkout. Not the operator's — that one has 29 uncommitted paths
#    and a stale .git/index.lock in it.
git -C ~/git/crypto worktree add ~/git/crypto-bots/researcher -b bots/researcher

# 3. a host, or the session has nowhere to start a runner
python -m omnigent server --port 6767 &
python -m omnigent host --server http://127.0.0.1:6767 &
#    put the host id in bot.yaml under workload_config.host

# 4. the bot
python -m army bots create ~/bots/net-fee-researcher/bot.yaml --config $C
python -m army bots activate net-fee-researcher --config $C
python -m army bots budget net-fee-researcher --grant 8 --config $C   # the real cap
python -m army bots run --interval 20 --config $C &

# 5. see it
python -m army bots serve --config $C --port 6769 &          # standalone
OMNIGENT_BOTS_URL=http://127.0.0.1:6769 python -m omnigent server --port 6767   # in-app
```

The bot's charter and queue live in `~/bots/net-fee-researcher/`, not in the
repository. The repository receives reports and nothing else.

## What stops it

- **A denied-path list checked outside the model**, after the session ends,
  against `git status`: `hitl/`, `registry/`, `tools/deploy_gate/`,
  `policies/`, `configs/`, `.github/`, `Makefile`. A match refuses the commit
  and blocks the run. The owner's channel, the roster, and the only thing that
  can arm real money are not things a prompt should be trusted with.
- **No push.** Local commits on `bots/researcher`; the operator merges.
- **A budget cap**, which is the real spend gate — this workload refills its own
  queue only when a person answers, but a workload that reported work every
  time would have no idle streak to back it off.
- **No broker key on this control plane**, so `spend`, `execute_order` and
  `add_dependency` are refused outright rather than gated.

## What does not contain it, stated plainly

The worktree is convenience, not containment. That repository's own
`docs/OMNIGENT-INTERFACE.md` §10.2 records a session that was handed `/tmp` as
its workspace and recursively read `~/git/crypto` anyway: Omnigent's file tools
respect an environment root, and the vendor CLI's native tools do not. Each run
records the sandbox profile it was *intended* to have, and records that nothing
enforces it at the process level on this build. Real containment needs a
separate macOS principal.

## What the first night broke

Three defects, all in Bot mode, none of which the heartbeat demo could have
found — see the commit `feat(bots): a research workload, and three defects a
real project found`. The documented `workload_config` shape crashed the
workload constructor; the worktree helper failed open; and the sandbox profile
had no caller. A real repository found all three in under an hour.
