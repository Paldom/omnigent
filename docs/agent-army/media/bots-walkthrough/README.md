# Recording the Bot mode walkthrough

Everything in `bots-walkthrough.mp4` is real: a live server, a real SQLite
file, and an approval answered through the same bound path the CLI uses. The
only addition is the caption strip, injected as an overlay so the narration
sits beside the product rather than replacing it.

```bash
# 1. a fleet in every state at once
python docs/agent-army/media/bots-walkthrough/seed.py /tmp/demo/army.db
printf '[army]\nstate_path = "/tmp/demo/army.db"\n' > /tmp/demo/army.toml

# 2. the page
python -m army bots serve --config /tmp/demo/army.toml --port 6810
#    prints the link, token included

# 3. the recording
export ARMY_CONFIG=/tmp/demo/army.toml
export PLAYWRIGHT_CHROME="…/Google Chrome for Testing"   # not the headless shell
node docs/agent-army/media/bots-walkthrough/walkthrough.mjs "$TOKEN" 6810 /tmp/demo/video

# 4. mp4 and a gif of the middle
# 15fps and crf 28: a mostly-static page compresses hard, and the repo's
# large-file hook stops at 1000 KB. Text stays legible; motion is minimal.
ffmpeg -i /tmp/demo/video/*.webm -vf "fps=15,scale=1200:-2:flags=lanczos" \
  -c:v libx264 -preset veryslow -crf 28 -pix_fmt yuv420p -movflags +faststart -an \
  bots-walkthrough.mp4
ffmpeg -ss 28 -t 20 -i bots-walkthrough.mp4 \
  -vf "fps=10,scale=760:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" \
  bots-walkthrough.gif
```

## Why the seed is fussy

A live run outranks any schedule — that is the point of deriving status. So a
tick taken while the whole fleet is active gives a run to whichever bot it
happens to pick and masks every other state behind `running`. `seed.py`
therefore activates one bot at a time: scout gets a real history of three
settled iterations, harvester is left mid-question, and only then is everyone
else released into their own states.

It also ticks *forward* from `now`. A bot's first wake is its activation, so
ticking at an earlier clock finds nothing due and leaves the ledger empty —
which is how the first cut of this recording ended up narrating "every run
carries a classified outcome" over the words *it has not run yet*.

## What the recording found

The walkthrough is a test. Recording it caught a defect nothing else had:
Chrome sends `Origin: null` for an ordinary form POST from a plain-HTTP page,
and the cross-site check treated that as hostile — so every approve button
returned 403 for every real user. `test_a_browsers_own_form_post_is_not_mistaken_for_a_cross_site_one`
is that bug, pinned.
