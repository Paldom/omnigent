"""Telling a person, where they already are.

Every human-in-the-loop round trip in this system bottlenecks on somebody
remembering to open a web page. That is the gap an operator feels hourly: a bot
parked on a question is *correct* — it holds no vendor seat, no session, no
process — and completely invisible. The fleet works and nobody knows.

Lives in ``army/`` rather than ``army/bots/`` because the arrow points one way:
Bot mode may import the core, never the reverse, and ``army/config.py`` has to
be able to read this table. Nothing here knows what a bot is — it signs a
string and posts JSON.

## What this is, and firmly is not

**Outbound only.** One webhook, one URL, best-effort. The chat integration that
rots is always the inbound half: bot users, event subscriptions, message
parsing, threading state. So it is not built. A Slack or Buzz incoming-webhook
URL is the entire integration surface, and when it is absent nothing here runs.

**A nudge, never an authority.** The row in the approval store is the truth.
This says "something is waiting" and carries a link; the message itself
authorises nothing, decides nothing, and can be lost without consequence. That
is what keeps the doctrine intact — a verdict binds to an action hash, and no
amount of chat can stand in for one.

## The link is a confirm page, not a button

The trap this design exists to avoid: **chat clients GET every URL they
unfurl.** A one-click `?approve=yes` link is therefore clicked by the preview
crawler, seconds after posting, from inside your network. An approval that a
link-preview bot can grant is not an approval.

So the token grants *reaching a page*, not deciding. `GET` renders the question
and two buttons; only a `POST` carrying the token decides. A crawler fetching
the URL sees the question and changes nothing.

The token is scoped to one approval and expires. It is deliberately not the
page's global token, which authorises answering *everything* — pasting that
into a chat room would hand the room the fleet.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from hashlib import sha256

_logger = logging.getLogger(__name__)

#: How long a link from a chat message stays usable. Long enough to answer
#: after lunch, short enough that a message in a channel somebody left open for
#: a month is not a standing key to the fleet.
LINK_TTL_S = 7 * 86_400

#: How long to wait on the webhook. Short: this is a courtesy on the tick's
#: critical path, and a slow chat provider must not hold up the loop.
TIMEOUT_S = 5.0


class LinkRefused(RuntimeError):
    """A link token that does not verify, has expired, or is for another thing."""


@dataclass(frozen=True)
class Egress:
    """Where to nudge, and what to sign links with.

    :param webhook_url: Where to POST. Empty disables the whole thing.
    :param secret: Signs approval links. Empty disables links but still nudges.
    :param public_url: How a person reaches the bots page from wherever the
        webhook lands — a tailnet name, usually. Without it the nudge carries
        no link, which is still better than a link to ``127.0.0.1``.
    """

    webhook_url: str = ""
    secret: str = ""
    public_url: str = ""

    def enabled(self) -> bool:
        """Whether there is anywhere to send."""
        return bool(self.webhook_url)

    def link_for(self, approval_id: str, *, now: int) -> str:
        """
        A scoped, expiring URL for one approval.

        :param approval_id: The approval this link answers.
        :param now: Epoch seconds.
        :returns: The URL, or ``""`` when links are not configured.
        """
        if not (self.secret and self.public_url):
            return ""
        return f"{self.public_url.rstrip('/')}/approve?t={mint(self.secret, approval_id, now=now)}"

    def notify(self, *, bot: str, question: str, evidence: dict, link: str) -> None:
        """
        Say that something is waiting. Never raises.

        Failure is logged and dropped on purpose: the approval row is the
        truth, and a chat provider having a bad afternoon must not fail an
        iteration or block the tick. A nudge that can take the fleet down is
        worse than no nudge.

        :param bot: Whose question it is.
        :param question: What is being asked.
        :param evidence: What the question is based on.
        :param link: Where to answer it, or ``""``.
        """
        if not self.enabled():
            return
        payload = json.dumps(
            {
                "text": f"{bot} is waiting on you: {question}" + (f"\n{link}" if link else ""),
                "bot": bot,
                "question": question,
                # Bounded: an evidence blob can carry a whole diff, and a chat
                # message is not where that belongs.
                "evidence": {k: str(v)[:200] for k, v in list(evidence.items())[:8]},
                "link": link,
            }
        ).encode()
        request = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _logger.warning("could not nudge about %s: %s", bot, exc)


def mint(secret: str, approval_id: str, *, now: int) -> str:
    """
    Sign a link token for one approval.

    :param secret: The signing key.
    :param approval_id: What the token answers.
    :param now: Epoch seconds.
    :returns: The token.
    """
    body = f"{approval_id}:{now + LINK_TTL_S}"
    signature = hmac.new(secret.encode(), body.encode(), sha256).hexdigest()[:32]
    return f"{base64.urlsafe_b64encode(body.encode()).decode().rstrip('=')}.{signature}"


def approval_in(secret: str, token: str, *, now: int) -> str:
    """
    The approval a token is for, if it verifies and has not expired.

    :param secret: The signing key.
    :param token: What arrived in the URL.
    :param now: Epoch seconds.
    :returns: The approval id.
    :raises LinkRefused: On anything that does not verify.
    """
    if not secret:
        raise LinkRefused("this control plane signs no links")
    encoded, _, signature = token.partition(".")
    if not signature:
        raise LinkRefused("that link is malformed")
    try:
        body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise LinkRefused("that link is malformed") from exc
    expected = hmac.new(secret.encode(), body.encode(), sha256).hexdigest()[:32]
    # Constant time: the token is short enough that a byte-at-a-time comparison
    # is a genuine oracle over a fast link.
    if not hmac.compare_digest(signature, expected):
        raise LinkRefused("that link was not signed by this control plane")
    approval_id, _, expiry = body.rpartition(":")
    if not expiry.isdigit() or now >= int(expiry):
        raise LinkRefused("that link has expired; open the bots page instead")
    return approval_id
