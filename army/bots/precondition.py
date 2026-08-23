"""Cheap, model-free checks that decide whether a body is worth spawning.

A continuous bot must declare one. Without it, discovering there is nothing to
do costs a full vendor turn every interval — backoff bounds how often that
happens but never makes it free, and ten continuous bots at an hourly floor
still burn about 240 turns a day doing nothing.

The rule that makes this safe: **a bot names a precondition, it never defines
one.** Registration happens in the operator's own process at startup, so a bot
invented at 3am by another bot can only choose from checks a person already
wrote. A definition format that let a bot supply a shell command would be a
capability escalation wearing a scheduler's clothes.

A tick that does not dispatch a body is not an iteration. No run exists, so
nothing reaches ``EVALUATING`` and nothing asks a human — which is why this
does not violate the standing rule that every iteration ends in an approval.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

#: A precondition takes the bot's configured arguments and answers one
#: question: is there anything to do? It must not call a model, must not
#: perform an external effect, and must return quickly — it runs inside the
#: tick, once per due bot.
Precondition = Callable[[dict[str, Any]], bool]


class UnknownPrecondition(LookupError):
    """Raised when a bot names a check nobody registered."""


class PreconditionRegistry:
    """The checks this deployment will run before spawning a body.

    :param checks: Initial ``{name: callable}``, or ``None`` for the built-ins.
    """

    def __init__(self, checks: dict[str, Precondition] | None = None) -> None:
        self._checks: dict[str, Precondition] = dict(BUILTIN if checks is None else checks)

    def register(self, name: str, check: Precondition) -> None:
        """
        Add a check bots may name.

        :param name: What a bot definition writes.
        :param check: The callable.
        :raises ValueError: If the name is already taken. Silently replacing
            one would let a later import change what an activated bot does.
        """
        if name in self._checks:
            raise ValueError(f"a precondition named {name!r} is already registered")
        self._checks[name] = check

    def evaluate(self, name: str, config: dict[str, Any]) -> bool:
        """
        Run a named check.

        A check that raises is treated as *false*, not as an error to
        propagate: the tick's job is to decide whether to spend a vendor turn,
        and "the check is broken" is not a reason to spend one. The exception
        is logged so a permanently broken check does not just look like a quiet
        bot.

        :param name: The check to run.
        :param config: The bot's arguments for it.
        :returns: Whether there is work.
        :raises UnknownPrecondition: If nothing is registered under *name*.
        """
        check = self._checks.get(name)
        if check is None:
            known = ", ".join(sorted(self._checks)) or "none"
            raise UnknownPrecondition(f"no precondition named {name!r}; registered: {known}")
        try:
            return bool(check(config))
        except Exception:
            _logger.exception("precondition %r raised; treating it as 'no work'", name)
            return False

    def __contains__(self, name: object) -> bool:
        return name in self._checks

    def names(self) -> list[str]:
        """Every registered name, sorted, for an error message or the CLI."""
        return sorted(self._checks)


def _always(_config: dict[str, Any]) -> bool:
    """There is always work.

    For a bot whose mission genuinely has no cheap check — a periodic report,
    say. Naming it explicitly is the point: it makes "this bot pays a vendor
    turn every wake" a visible choice rather than an oversight.
    """
    return True


def _never(_config: dict[str, Any]) -> bool:
    """There is never work. Useful for parking a bot without pausing it."""
    return False


def _queue_file_has_work(config: dict[str, Any]) -> bool:
    """
    Whether a plain-text queue file holds an unclaimed line.

    The same convention ``army.workloads.demo`` uses: blank lines, comments,
    ``taken:`` and ``done:`` do not count.

    :param config: Needs ``path``.
    :returns: Whether any line is still waiting.
    """
    path = Path(str(config.get("path", ""))).expanduser()
    if not path.exists():
        return False
    for line in path.read_text().splitlines():
        task = line.strip()
        if task and not task.startswith(("#", "done:", "taken:")):
            return True
    return False


def _path_exists(config: dict[str, Any]) -> bool:
    """
    Whether a path is present, for a bot that watches a drop directory.

    :param config: Needs ``path``. With ``non_empty`` set, a directory must
        also contain something.
    :returns: Whether the path is there.
    """
    path = Path(str(config.get("path", ""))).expanduser()
    if not path.exists():
        return False
    if config.get("non_empty") and path.is_dir():
        return any(path.iterdir())
    return True


def _inbox_has_unacked(config: dict[str, Any]) -> bool:
    """
    Whether this bot has undelivered mail.

    Wired by the supervisor, which supplies ``pending`` from the delivery
    scan it already ran. Reading the table again here would double the query
    every tick for an answer the caller has in hand.

    :param config: Supplied by the tick, carrying ``pending``.
    :returns: Whether anything is queued for this bot.
    """
    return bool(config.get("pending"))


#: What every deployment gets without registering anything. Deliberately dull:
#: a built-in that touched the network or ran a subprocess would be a check
#: with a side effect, which is not a check.
BUILTIN: dict[str, Precondition] = {
    "always": _always,
    "never": _never,
    "queue_file_has_work": _queue_file_has_work,
    "path_exists": _path_exists,
    "inbox_has_unacked": _inbox_has_unacked,
}
