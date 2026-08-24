"""Who may say which browser a session drives.

The session label ``omnigent.browser.profile`` decides which Chromium — and so
which cookie jar, which logged-in identity — a session's ``browser_*`` actions
reach. On its own that makes it a **bearer capability**: anything that can
create a session naming another bot's profile drives that bot's browser as that
bot, and the audit trail attributes it to the victim.

That matters because a bot's body is an ordinary session with a shell in it. A
bot that knows another bot's slug could otherwise ``curl`` this server, open a
session labelled with the other bot's profile, and read its logged-in pages.

So the label is gated on a secret the fleet's *control plane* holds and its
*bots* are not given: the same token that authorises answering an approval,
which lives in a directory :data:`army.bots.isolation.CONTROL_PLANE_DIRS`
withholds from every bot's sandbox.

## What this does not do

It does not survive a bot that can read that file. This build applies no
Seatbelt profile and a native vendor CLI's own file tools are not confined —
the sandbox recorded on every run says so in those words. A bot with an
unconfined shell can read the token and forge the label, and no check on this
side of the boundary changes that.

What it does is stop the cheap version: an agent steered by a hostile page, or
one using only its sanctioned tools, cannot name a browser that is not its own.
That is worth having and it is not the same as isolation, so it is written down
here rather than implied by the absence of a comment.
"""

from __future__ import annotations

import hmac
from pathlib import Path

#: Where the control plane's shared secret lives. The same file
#: ``army.bots.web`` mints and reads.
TOKEN_FILE = Path.home() / ".omnigent" / "army" / "web-token"

#: The header a caller presents to prove it is the control plane.
CONTROL_HEADER = "X-Omnigent-Control"

#: The label being protected.
BROWSER_PROFILE_LABEL = "omnigent.browser.profile"


def control_token() -> str | None:
    """
    The control plane's secret, or ``None`` when Bot mode has never run.

    :returns: The token, or ``None``.
    """
    try:
        secret = TOKEN_FILE.read_text().strip()
    except OSError:
        return None
    return secret or None


def may_set_browser_profile(supplied: str | None) -> bool:
    """
    Whether a caller presenting *supplied* may label a session's browser.

    Compared in constant time. Answers ``False`` when no token exists at all:
    without a control plane there is nothing that legitimately sets this label,
    so the safe reading of "no secret configured" is "nobody", not "everybody".

    :param supplied: What the caller sent in :data:`CONTROL_HEADER`.
    :returns: Whether the label may be set.
    """
    expected = control_token()
    if expected is None or not supplied:
        return False
    return hmac.compare_digest(supplied, expected)
