"""Hard gates as capability boundaries, not as policies.

A policy that returns ASK is good defence in depth and the right place to put a
question. It is not a boundary. Evaluation order proves a session-level policy
cannot override an admin one; it does not prove that every route to an action
goes through the policy engine — and several plainly do not:

- a native harness has its own shell and its own tools;
- an agent can edit a dependency manifest directly instead of calling a tool;
- an MCP server can expose a write path nobody enumerated;
- a shell holding a GitHub token can call ``gh`` or raw HTTP without touching
  whatever the intended merge tool was;
- no semantic rule reliably infers that a given command ends up spending money.

So the boundary is drawn where it can actually hold: **agents do not have the
credential**. Anything irreversible goes through a broker in a different
process, holding secrets the agent's environment never sees, and the broker
refuses to act without an owner approval bound to that exact operation.

Binding matters. An approval is a signature over a digest of the operation
itself, so a grant for "merge PR 42" cannot be replayed as "merge PR 43", and
it expires — an approval you gave last week is not consent for tonight.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any

#: Operations no automated approval may ever stand in for. The owner grants
#: these one at a time, and the grant is bound to one operation and one
#: expiry. This list is the settled risk posture, not a default to tune.
ALWAYS_OWNER: frozenset[str] = frozenset(
    {
        "add_dependency",  # a new dependency is new code you did not read
        "execute_order",  # real funds
        "spend",  # any unattended spend
    }
)

#: Non-LLM secrets the deployment is allowed to hold. "No API keys" is about
#: model vendors — merging a PR genuinely needs a GitHub credential, and
#: pretending otherwise just moves the secret somewhere undeclared. Anything
#: not on this list is refused rather than quietly permitted.
ALLOWED_SECRETS: frozenset[str] = frozenset(
    {
        "ARMY_GITHUB_TOKEN",
        "ARMY_BROKER_KEY",
        "ARMY_TAILSCALE_KEY",
    }
)

#: How long a grant stays usable. Long enough to answer from your phone and
#: have the loop act on it; short enough that a leaked grant is not a standing
#: permission.
DEFAULT_GRANT_SECONDS = 3600


class GateRefused(RuntimeError):
    """A gated operation was refused. The message says which rule refused it."""


def digest(operation: str, parameters: dict[str, Any]) -> str:
    """
    Fingerprint one exact operation.

    Sorted keys so the same operation always fingerprints the same way, and the
    operation name is inside the digest so parameters alone cannot be reused
    under a different verb.

    :param operation: What is being done, e.g. ``"merge_pr"``.
    :param parameters: Its arguments, e.g. ``{"repo": "x/y", "number": 42}``.
    :returns: A hex digest identifying this operation and no other.
    """
    payload = json.dumps(
        {"operation": operation, "parameters": parameters},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class Grant:
    """An owner's permission for one operation, for a while.

    :param operation: The verb granted.
    :param digest: Fingerprint of the exact operation, from :func:`digest`.
    :param expires_at: Unix epoch seconds after which it is worthless.
    :param signature: HMAC over the three fields above, made with a key the
        agents' environment does not contain.
    """

    operation: str
    digest: str
    expires_at: int
    signature: str

    def to_token(self) -> str:
        """Render the grant as one line, for pasting into a chat reply."""
        return f"{self.operation}.{self.digest}.{self.expires_at}.{self.signature}"

    @staticmethod
    def from_token(token: str) -> Grant:
        """
        Parse a grant token.

        :param token: A string from :meth:`to_token`.
        :returns: The grant.
        :raises GateRefused: If the token is not well formed.
        """
        parts = token.strip().split(".")
        if len(parts) != 4:
            raise GateRefused("malformed grant token")
        operation, fingerprint, expires, signature = parts
        try:
            expires_at = int(expires)
        except ValueError as exc:
            raise GateRefused("malformed grant expiry") from exc
        return Grant(operation, fingerprint, expires_at, signature)


class Broker:
    """Holds the credentials agents do not, and will not act without a grant.

    The broker is meant to run as its own process under its own account. Put it
    in the same process as the agents and the boundary is gone — the point is
    not the code in this class, it is that ``ARMY_BROKER_KEY`` and
    ``ARMY_GITHUB_TOKEN`` are absent from every environment an agent can read.

    :param key: Signing key. Defaults to ``ARMY_BROKER_KEY``.
    :raises GateRefused: If no key is available — an unsigned broker would
        approve everything, so it refuses to exist instead.
    """

    def __init__(self, key: str | None = None) -> None:
        resolved = key or os.environ.get("ARMY_BROKER_KEY")
        if not resolved:
            raise GateRefused(
                "no ARMY_BROKER_KEY: refusing to start a broker that cannot verify grants"
            )
        self._key = resolved.encode()

    def sign(
        self,
        operation: str,
        parameters: dict[str, Any],
        *,
        ttl_seconds: int = DEFAULT_GRANT_SECONDS,
        now: int | None = None,
    ) -> Grant:
        """
        Issue a grant. Only the owner should be able to reach this.

        :param operation: The verb being granted.
        :param parameters: The exact arguments being granted.
        :param ttl_seconds: How long the grant lasts.
        :param now: Unix epoch seconds; defaults to the clock.
        :returns: The signed grant.
        """
        stamp = int(time.time()) if now is None else now
        fingerprint = digest(operation, parameters)
        expires_at = stamp + ttl_seconds
        return Grant(
            operation=operation,
            digest=fingerprint,
            expires_at=expires_at,
            signature=self._sign(operation, fingerprint, expires_at),
        )

    def check(
        self,
        operation: str,
        parameters: dict[str, Any],
        grant: Grant | None,
        *,
        now: int | None = None,
    ) -> None:
        """
        Refuse unless this exact operation was granted and the grant is live.

        :param operation: What is about to happen.
        :param parameters: Its exact arguments.
        :param grant: The grant offered, or ``None``.
        :param now: Unix epoch seconds; defaults to the clock.
        :raises GateRefused: With the reason, whenever the check does not pass.
        """
        stamp = int(time.time()) if now is None else now
        if grant is None:
            raise GateRefused(f"{operation} needs an owner grant and none was supplied")
        if grant.operation != operation:
            raise GateRefused(f"grant is for {grant.operation!r}, not {operation!r}")
        expected = digest(operation, parameters)
        if not hmac.compare_digest(grant.digest, expected):
            raise GateRefused(
                f"grant does not match these arguments; it was issued for a different {operation}"
            )
        if stamp >= grant.expires_at:
            raise GateRefused(f"grant expired {stamp - grant.expires_at}s ago; ask again")
        expected_signature = self._sign(operation, grant.digest, grant.expires_at)
        if not hmac.compare_digest(grant.signature, expected_signature):
            raise GateRefused("grant signature does not verify")

    def _sign(self, operation: str, fingerprint: str, expires_at: int) -> str:
        """Compute the HMAC over a grant's fields."""
        message = f"{operation}.{fingerprint}.{expires_at}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()


def assert_agent_environment_is_clean(env: dict[str, str] | None = None) -> list[str]:
    """
    Report model-vendor and broker secrets reachable from an agent's environment.

    Run this against the environment a session is actually spawned with, not
    the shell you typed in — an empty shell proves nothing about what the
    runner passes down. A non-empty result means the capability boundary is not
    where you think it is.

    :param env: Environment to inspect; defaults to this process's.
    :returns: Names of variables that should not be reachable, sorted.
    """
    environ = os.environ if env is None else env
    leaked = []
    for name in environ:
        upper = name.upper()
        if upper in ALLOWED_SECRETS:
            # Present by design in the broker's own environment. If one of
            # these shows up in an *agent's* environment, the broker is running
            # in the wrong process, which this check cannot see from here.
            continue
        if upper.endswith(("_API_KEY", "_TOKEN")):
            leaked.append(name)
    return sorted(set(leaked))
