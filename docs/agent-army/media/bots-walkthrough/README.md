# Recording the Bot mode walkthrough

Everything in `bots-walkthrough.mp4` is real: a live Omnigent server, a live
control plane in its own process, a real SQLite file, a real $40 grant minted
and spent, and a real bot activated. The only addition is the caption strip,
injected as an overlay so the narration sits beside the product rather than
replacing it.

```bash
# 1. a fleet in every state at once
mkdir -p /tmp/demo
python docs/agent-army/media/bots-walkthrough/seed.py /tmp/demo/army.db
printf '[army]\nstate_path = "/tmp/demo/army.db"\n' > /tmp/demo/army.toml

# 2. the control plane. The key is what makes the owner path answerable —
#    without it the signature card says so instead of offering a button.
ARMY_BROKER_KEY=demo-owner-key \
  python -m army bots serve --config /tmp/demo/army.toml --port 6768

# 3. the app, which proxies to it on loopback
python -m omnigent server --port 6767
pnpm --dir web dev            # or whatever port your vite is on

# 4. the recording
export ARMY_CONFIG=/tmp/demo/army.toml
export PLAYWRIGHT_CHROME="…/Google Chrome for Testing"   # not the headless shell
export PLAYWRIGHT_CORE="…/node_modules/playwright-core"  # not a repo dependency
node docs/agent-army/media/bots-walkthrough/walkthrough.mjs \
  http://localhost:5199 /tmp/demo/video

# 5. mp4, and a gif of the signature stretch
# 15fps and crf 30: a mostly-static page compresses hard, and the repo's
# large-file hook stops at 1000 KB. Text stays legible; motion is minimal.
ffmpeg -i /tmp/demo/video/*.webm -vf "fps=15,scale=1200:-2:flags=lanczos" \
  -c:v libx264 -preset veryslow -crf 30 -pix_fmt yuv420p -movflags +faststart -an \
  bots-walkthrough.mp4
ffmpeg -ss 22 -t 22 -i bots-walkthrough.mp4 \
  -vf "fps=10,scale=760:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" \
  bots-walkthrough.gif
```

**The recording is destructive.** It signs the pending spend and activates the
proposal, so re-run `seed.py` before each take.

## Why the seed is fussy

A live run outranks any schedule — that is the point of deriving status. So a
tick taken while the whole fleet is active gives a run to whichever bot it
happens to pick and masks every other state behind `running`. `seed.py`
therefore activates one bot at a time: scout gets a real history of three
settled iterations, harvester is left mid-question, treasurer is parked on a
`spend` nothing but the owner path can answer, and only then is everyone else
released into their own states.

It also ticks *forward* from `now`. A bot's first wake is its activation, so
ticking at an earlier clock finds nothing due and leaves the ledger empty —
which is how the first cut of this recording ended up narrating "every run
carries a classified outcome" over the words *it has not run yet*.

## What the recording found

The walkthrough is a test, and it has now caught two defects nothing else did.

**Chrome sends `Origin: null`** for an ordinary form POST from a plain-HTTP
page, and the cross-site check treated that as hostile — so every approve
button returned 403 for every real user.
`test_a_browsers_own_form_post_is_not_mistaken_for_a_cross_site_one` is that
bug, pinned.

**Answering something emptied the middle column.** Signing the spend cleared the
selection, the effect re-picked it from a roster that had not refetched yet,
and then the refetch removed it — leaving a selection pointing at a row that no
longer existed and a blank column where the next piece of work should have
been. The guard was written to survive a loading gap and therefore never fired
when the list legitimately emptied, which is the case that actually happens.
`moves on when the selected row disappears underneath it` is that bug, pinned —
and it fails against the old guard, which is the only reason to believe it.
