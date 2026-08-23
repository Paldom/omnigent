"""Resolving the workload a bot names, once per process.

``Supervisor`` is constructed with exactly one :class:`~army.workload.Workload`
and opens runs only from that object's ``acquire()``. Bots each name their own,
so a fleet of heterogeneous bots needs somewhere to look one up — and it needs
to be the *same instance* each time, because a workload may cache resolved
agent ids, host ids and open files.

Importing is the risky part. A dotted path in a bot definition is arbitrary
code, so this resolves only paths the operator allowed, and says clearly which
bot asked for something that is not on the list.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from army.workload import Workload

_logger = logging.getLogger(__name__)


class WorkloadRefused(RuntimeError):
    """A bot named a workload this deployment will not load."""


class WorkloadRegistry:
    """Loads and caches the workloads bots name.

    :param allow: Dotted ``module:Class`` paths that may be imported. ``None``
        means "anything under ``army.workloads``", which is the safe default
        for a deployment that has not thought about it yet. An empty set
        refuses everything, which is the right posture for a box where bots can
        be created at runtime by other bots.
    """

    #: Prefixes always permitted, because they ship with the loop and a person
    #: reviewed them. Anything else is a decision the operator has to make.
    DEFAULT_PREFIXES: tuple[str, ...] = ("army.workloads.", "army.bots.workloads.")

    def __init__(self, allow: set[str] | None = None) -> None:
        self.allow = allow
        self._instances: dict[str, Workload] = {}

    def permits(self, path: str) -> bool:
        """
        Whether this deployment will import *path*.

        :param path: A dotted ``module:Class`` reference.
        :returns: Whether it is allowed.
        """
        if self.allow is None:
            return path.startswith(self.DEFAULT_PREFIXES)
        return path in self.allow

    def resolve(self, path: str, options: dict[str, Any] | None = None) -> Workload:
        """
        Import and construct a workload, or return the one already built.

        Cached on the path *and* its options, so two bots naming the same class
        with different configuration get two instances while two bots naming it
        identically share one. Sharing an instance across differently-configured
        bots is the bug this shape prevents: the second bot would silently run
        with the first one's queue file.

        :param path: Dotted ``module:Class``.
        :param options: Constructor keyword arguments.
        :returns: The workload.
        :raises WorkloadRefused: If the path is not permitted or will not load.
        """
        if not self.permits(path):
            raise WorkloadRefused(
                f"{path!r} is not an allowed workload; add it to [bots] allow_workloads"
            )
        if ":" not in path:
            raise WorkloadRefused(f"workload must be 'module:Class', got {path!r}")
        key = f"{path}|{sorted((options or {}).items())}"
        cached = self._instances.get(key)
        if cached is not None:
            return cached
        module_name, _, attribute = path.partition(":")
        try:
            module = importlib.import_module(module_name)
            factory = getattr(module, attribute)
        except (ImportError, AttributeError) as exc:
            raise WorkloadRefused(f"cannot load workload {path!r}: {exc}") from exc
        try:
            workload: Workload = factory(**(options or {}))
        except TypeError as exc:
            raise WorkloadRefused(f"cannot construct workload {path!r}: {exc}") from exc
        self._instances[key] = workload
        return workload

    def for_bot(self, bot: Any) -> Workload:
        """
        Resolve the workload a bot names, with its own configuration.

        :param bot: A :class:`army.bots.model.Bot`.
        :returns: Its workload.
        :raises WorkloadRefused: Naming the bot, so the operator knows which
            definition to fix rather than which import failed.
        """
        try:
            return self.resolve(bot.workload, bot.workload_config)
        except WorkloadRefused as exc:
            raise WorkloadRefused(f"bot {bot.slug!r}: {exc}") from exc
