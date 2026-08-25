# A bot on a stealth identity

A bot's browser is ordinary Chromium by default. Give it a provisioned
[CloakBrowser](https://github.com/CloakHQ/CloakBrowser) identity and it drives
that instead — same Playwright API, a device pinned by seed, and a `user-data`
directory that keeps a real signed-in session between runs.

The point is not evading anything. It is that **a session survives**: a login
you complete by hand on Monday is still a login on Friday, so a bot watching a
page behind one can keep watching it without asking you again every morning.

## Why a bot needs the seed, specifically

With no `--fingerprint=<seed>` the binary rolls a fresh device fingerprint on
every launch. For an interactive session that is a nuisance; for a bot it is
the whole failure. A watcher exists to open the same page as the same visitor
for months — a new device each run means re-verification each run, which is
exactly the thing the persistent profile was supposed to prevent.

So the gateway refuses to launch an identity whose seed is unset rather than
starting something that looks like it works.

## Setting one up

The three skills own this; the gateway only reads what they produce.

```bash
npx skills add Paldom/playwright-stealth      # once per machine

# toolchain (pip wrapper + ~200 MB patched binary)
python3 .agents/skills/playwright-stealth-setup/scripts/setup_check.py --install

# one identity, named after the bot's own browser profile
python3 .agents/skills/playwright-stealth-identity/scripts/identity.py \
    --identity bot-kraken-fee-watch --init
```

The name is the whole wiring. A bot's browser profile is `persist:bot-<slug>`,
which canonicalises to `bot-<slug>`; an identity directory of that name under
`.stealth/` is what the gateway looks for.

**Opt-in by existence, and deliberately so.** There is no key in a bot
definition to set, which means there is none to forget and none for a
bot-proposed definition to ask for. Provisioning an identity is an operator
act; a bot inherits whatever was provisioned under its own name.

The server needs the package too, and finds identities relative to its working
directory unless told otherwise:

```bash
uv pip install -U 'cloakbrowser[geoip]' --python .venv/bin/python3
OMNIGENT_STEALTH_ROOT=/path/to/.stealth omnigent server
```

## What changes, and what does not

Everything the gateway already enforced still applies, because none of it is
about which binary is running:

- `file:`, loopback and private addresses are refused, on every request.
- The executor refuses to type into password and one-time-code fields.
- Refs are verified against what the snapshot named before a click lands.
- Taking the wheel refuses the bot's actions — including reads.

What changes is that the session is real. The screenshot in the Screen panel is
a page that is genuinely logged in, which is worth saying out loud: **a person
watching a stealth bot may be looking at their own account.**

Headful is not negotiable. The identity's `profile.toml` sets
`headless = false` because headless leaks GPU, display and timing signals even
with the C++ patches applied — and a gateway that quietly forced headless would
be defeating the thing it was asked to run. The practical consequence is that
the browser window is on the operator's screen: when you take the wheel, you
type into that window directly, and Omnigent's job is to have stopped the bot
touching it.

## The login handoff, end to end

This is the flow the whole thing exists for, and it needs no credential to
reach a bot:

1. The bot navigates to a page behind a sign-in and snapshots it.
2. It tries the password field. **The executor refuses** — the refusal is a
   mechanism, not an instruction the page can argue with.
3. It reports `LOGIN`, names what is being asked for, and parks. No baseline is
   written, because nothing was read.
4. You see the question, open the Screen panel, and take the wheel. The bot's
   actions — including snapshots — are refused from that moment.
5. You sign in, in the window that is already open.
6. You hand back. The next iteration opens on the same profile, still signed
   in, because the seed and the `user-data` directory both survived.

Step 5 is the part no screenshot-beside-a-fetch design can offer: you are
signing into the browser the bot actually uses, not a different one.

## Authorized use only

The skills say this and it bears repeating here, because a fleet makes it
easier to forget: automate properties you own or are authorized to test,
respect Terms of Service and `robots.txt`, and do not use this for credential
abuse, mass account creation, or evading access controls you have no right to
bypass. A bot that runs unattended for months raises the stakes on getting that
judgement right once.
