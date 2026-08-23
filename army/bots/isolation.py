"""How far a bot can reach, and the honest limits of that on one Mac.

OpenBot gives every bot a container, a volume and a browser profile. That is
the right model and **it does not transfer**, for one specific reason: the
vendor CLIs are logged in on the host. Claude Code, Codex, Grok and the rest
hold subscription credentials in host config and the keychain. Put the bot in a
container and it has no auth; mount the credentials in and the boundary is
decorative while making theft easier.

So the isolation degrades deliberately, and the degradation is stated rather
than hidden:

===============  ==========================  ====================
Dimension        Here                        Status
===============  ==========================  ====================
Filesystem       git worktree per bot        real
Process          Seatbelt profile per bot    real
Browser          storage partition per bot   needs the Electron fix
Vendor configs   excluded from read paths    real, and load-bearing
Network          shared                      **not isolated**
Credentials      host, shared per vendor     forced by subscription auth
===============  ==========================  ====================

The row that is easy to skip is the vendor one. Every bot's shell can otherwise
read and invoke every vendor's CLI login, so a cooled Claude lane, a child's
carved budget and the router's vendor choice are all one ``codex`` subprocess
away from being bypassed. ``assert_agent_environment_is_clean`` does not catch
it, because it inspects environment variables and a CLI login lives in a config
file. Excluding those directories is what turns the vendor lane from an
accounting convention into a boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from army.bots.model import Bot

#: Vendor CLI configuration directories, relative to the home directory. A bot
#: that can read these can log in as that vendor and spend a subscription the
#: lane accounting believes is idle.
VENDOR_CONFIG_DIRS: tuple[str, ...] = (
    ".claude",
    ".codex",
    ".grok",
    ".kimi",
    ".antigravity",
    ".pi",
    ".config/claude",
    ".config/codex",
)

#: Also withheld: the control plane's own state, so a bot cannot edit the rows
#: that decide what it is allowed to do. A bot that can write ``approval_requests``
#: does not need an approval.
CONTROL_PLANE_DIRS: tuple[str, ...] = (".omnigent/army",)


@dataclass(frozen=True)
class SandboxProfile:
    """What one bot's process may touch.

    :param bot_slug: Whose profile this is.
    :param write_paths: Directories it may write to.
    :param read_paths: Directories it may read.
    :param deny_read_paths: Directories withheld even though they are inside a
        readable parent — the vendor logins, and the control plane's database.
    :param network: Whether network access is allowed. Shared, never isolated;
        recorded so the honest answer is in the profile rather than in prose.
    """

    bot_slug: str
    write_paths: tuple[str, ...]
    read_paths: tuple[str, ...]
    deny_read_paths: tuple[str, ...]
    network: bool = True

    def as_dict(self) -> dict[str, object]:
        """The profile as plain data, for a policy or a log line."""
        return {
            "bot": self.bot_slug,
            "write_paths": list(self.write_paths),
            "read_paths": list(self.read_paths),
            "deny_read_paths": list(self.deny_read_paths),
            "network": self.network,
            "network_isolated": False,
        }


def profile_for(
    bot: Bot,
    workspace: Path,
    *,
    home: Path | None = None,
    extra_read: tuple[str, ...] = (),
) -> SandboxProfile:
    """
    Build the sandbox profile for one bot.

    ``enforce_sandbox`` in Omnigent is a static factory with ``write_paths``
    fixed at registration, so per-bot paths are small code rather than
    configuration — this is that code, kept as data so the policy that applies
    it can stay dumb.

    :param bot: The bot.
    :param workspace: Its directory, which is the only thing it may write to.
    :param home: Home directory; defaults to the real one.
    :param extra_read: Additional readable paths an operator has allowed.
    :returns: The profile.
    """
    root = home or Path.home()
    return SandboxProfile(
        bot_slug=bot.slug,
        # One writable directory. A bot that can write outside its own
        # workspace can edit another bot's charter, which is a way to change
        # what that bot does without anyone approving a revision.
        write_paths=(str(workspace),),
        read_paths=(str(workspace), *extra_read),
        deny_read_paths=tuple(
            str(root / directory) for directory in (*VENDOR_CONFIG_DIRS, *CONTROL_PLANE_DIRS)
        ),
    )


# ── browsing ──────────────────────────────────────────────────────


class BrowsingRefused(RuntimeError):
    """An authenticated browse was refused because bots share a cookie jar."""


@dataclass
class BrowserPolicy:
    """Whether a bot may browse somewhere that knows who it is.

    Views are already keyed per conversation, but no Electron ``partition`` is
    set, so every bot likely shares one cookie jar. A bot logged into a site
    while sharing storage with nine others is a credential-blending accident,
    and the failure is silent — nothing errors, the wrong session is simply
    used.

    So authenticated browsing is refused until partitioning exists.
    Unauthenticated browsing stays allowed, because it cannot blend anything.

    :param partitioned: Whether per-bot storage partitions are in force. Set
        this ``True`` only when the Electron side actually passes a partition.
    :param allow_authenticated: Operator override, for a box where every bot is
        trusted equally and the owner has said so.
    """

    partitioned: bool = False
    allow_authenticated: bool = False

    def partition_for(self, bot: Bot) -> str:
        """
        The storage partition key for one bot.

        Derived from the bot id rather than the slug: a slug can be edited, and
        a bot whose cookie jar changes name has silently logged itself out of
        everything.

        :param bot: The bot.
        :returns: An Electron partition key.
        """
        return bot.browser_profile or f"persist:bot-{bot.id}"

    def check(self, bot: Bot, *, authenticated: bool) -> None:
        """
        Refuse an authenticated browse while the cookie jar is shared.

        :param bot: The bot about to browse.
        :param authenticated: Whether the page needs a logged-in session.
        :raises BrowsingRefused: When it would share credentials with the fleet.
        """
        if not authenticated or self.partitioned or self.allow_authenticated:
            return
        raise BrowsingRefused(
            f"{bot.slug} may not browse as a logged-in user yet: browser views are keyed "
            "per conversation but share one cookie jar, so its session would be visible "
            "to every other bot. Unauthenticated browsing is fine. Set "
            "[bots] browser_partitioned = true once the Electron side passes a partition."
        )


@dataclass
class Wheel:
    """Who is driving a bot's browser.

    Copied from OpenBot including the part people get wrong: while a human is
    driving, bot actions are **refused, not queued**. Queuing them means the bot
    resumes into a page that is no longer where it thinks it is, and acts on it.

    :param held_by: Who has control, or ``None`` when the bot does.
    :param taken_at: Epoch seconds control was taken.
    :param reason: Why, so the bot's channel can say.
    """

    held_by: str | None = None
    taken_at: int | None = None
    reason: str = ""
    #: Bots that asked for help, so a person can see who is stuck.
    _asked: set[str] = field(default_factory=set)

    def help_requested(self, bot: Bot) -> None:
        """Record that a bot cannot get past something itself."""
        self._asked.add(bot.slug)

    def waiting_for_help(self) -> list[str]:
        """Bots that have asked a person to take over."""
        return sorted(self._asked)

    def take(self, who: str, *, now: int, reason: str = "") -> None:
        """
        Take control of the browser.

        :param who: The person driving.
        :param now: Epoch seconds.
        :param reason: Why.
        """
        self.held_by = who
        self.taken_at = now
        self.reason = reason

    def release(self, bot: Bot | None = None) -> None:
        """
        Hand control back.

        :param bot: The bot that asked for help, so the request is cleared.
        """
        self.held_by = None
        self.taken_at = None
        self.reason = ""
        if bot is not None:
            self._asked.discard(bot.slug)

    def assert_bot_may_act(self, bot: Bot) -> None:
        """
        Refuse a bot action while a person is driving.

        :param bot: The bot trying to act.
        :raises BrowsingRefused: While control is taken.
        """
        if self.held_by is None:
            return
        raise BrowsingRefused(
            f"{self.held_by} is driving {bot.slug}'s browser"
            + (f" ({self.reason})" if self.reason else "")
            + ". Bot actions are refused rather than queued, because a queued action "
            "would run against whatever page the person navigated to."
        )
