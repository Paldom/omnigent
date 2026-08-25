"""Configuration: where the loop keeps its state, who it talks to, what it runs.

One TOML file, read with the stdlib parser. Search order is the explicit
``--config`` path, then ``./army.toml``, then ``~/.omnigent/army.toml``; the
first that exists wins, and every field has a working default so a fresh
install runs without one.

Secrets do not live here. The server token is read from ``ARMY_TOKEN`` so the
file stays safe to commit alongside the agent roster it configures.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomllib

from army.egress import Egress
from army.lanes import DEFAULT_LANES, DEFAULT_LIMIT_PHRASES
from army.workload import Workload

#: Where to look for the file when none is named, in order.
SEARCH_PATH: tuple[Path, ...] = (
    Path("army.toml"),
    Path.home() / ".omnigent" / "army.toml",
)


@dataclass
class Config:
    """Resolved configuration.

    :param server_url: Omnigent server root.
    :param token: Bearer token for a server with auth on, from ``ARMY_TOKEN``.
    :param state_path: SQLite file holding runs, commands and effects. This is
        the file that must survive a reboot; back it up.
    :param workload: Dotted path to the workload, e.g.
        ``"army.workloads.demo:DemoWorkload"``.
    :param workload_options: Keyword arguments for the workload's constructor.
    :param egress: Where to nudge a person about a waiting question. Absent
        is the off switch: a fleet with no webhook behaves as it did before.
    :param lanes: Per-harness concurrency caps.
    :param max_concurrent_runs: Iterations allowed in flight at once.
    :param limit_phrases: Text that means a vendor refused on quota. Vendor
        wording differs and changes, and a phrase that never matches fails
        silently, so this is configurable rather than compiled in.
    :param allow_workloads: Dotted ``module:Class`` paths a bot definition may
        name. ``None`` permits only the ones that ship with the loop, which is
        the right default on a box where a bot can propose another bot: a
        definition is data, and data that can name arbitrary importable code is
        not data any more.
    """

    server_url: str = "http://localhost:6767"
    token: str | None = None
    state_path: Path = Path.home() / ".omnigent" / "army" / "army.db"
    workload: str = "army.workloads.demo:DemoWorkload"
    workload_options: dict[str, Any] = field(default_factory=dict)
    egress: Egress = field(default_factory=Egress)
    lanes: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LANES))
    max_concurrent_runs: int = 3
    limit_phrases: tuple[str, ...] = DEFAULT_LIMIT_PHRASES
    allow_workloads: set[str] | None = None

    def load_workload(self) -> Workload:
        """
        Import and construct the configured workload.

        :returns: The workload instance.
        :raises ValueError: If the dotted path is malformed.
        :raises ImportError: If the module or attribute does not exist.
        """
        if ":" not in self.workload:
            raise ValueError(f"workload must be 'module:Class', got {self.workload!r}")
        module_name, _, attribute = self.workload.partition(":")
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute)
        return factory(**self.workload_options)  # type: ignore[no-any-return]


def load_config(path: Path | None = None) -> Config:
    """
    Read configuration, falling back to defaults for anything unset.

    :param path: Explicit file, or ``None`` to search :data:`SEARCH_PATH`.
    :returns: The resolved configuration.
    :raises FileNotFoundError: If *path* was given and does not exist.
    """
    if path is not None:
        if not path.exists():
            raise FileNotFoundError(f"no config at {path}")
        data = tomllib.loads(path.read_text())
    else:
        data = {}
        for candidate in SEARCH_PATH:
            if candidate.exists():
                data = tomllib.loads(candidate.read_text())
                break

    army = data.get("army", {})
    config = Config()
    if "server_url" in army:
        config.server_url = str(army["server_url"])
    if "state_path" in army:
        config.state_path = Path(str(army["state_path"])).expanduser()
    if "workload" in army:
        config.workload = str(army["workload"])
    if "max_concurrent_runs" in army:
        config.max_concurrent_runs = int(army["max_concurrent_runs"])
    if isinstance(data.get("workload_options"), dict):
        config.workload_options = dict(data["workload_options"])
    nudge = data.get("egress")
    if isinstance(nudge, dict):
        config.egress = Egress(
            webhook_url=str(nudge.get("webhook_url") or ""),
            secret=str(nudge.get("secret") or ""),
            public_url=str(nudge.get("public_url") or ""),
        )
    if isinstance(data.get("lanes"), dict):
        config.lanes = {str(k): int(v) for k, v in data["lanes"].items()}
    rate_limit = data.get("rate_limit")
    if isinstance(rate_limit, dict) and isinstance(rate_limit.get("phrases"), list):
        # Replaces rather than extends: an operator who has watched their own
        # vendors refuse knows better than this file's defaults, and a list you
        # cannot narrow is one you cannot debug.
        config.limit_phrases = tuple(str(p).lower() for p in rate_limit["phrases"])

    bots = data.get("bots")
    if isinstance(bots, dict) and isinstance(bots.get("allow_workloads"), list):
        # An explicit list replaces the built-in prefixes rather than extending
        # them: an operator naming what may run wants that list to be the whole
        # answer, not a suggestion on top of defaults they did not write.
        config.allow_workloads = {str(path) for path in bots["allow_workloads"]}

    # Read last so an environment token always beats a file that should not
    # have contained one in the first place.
    config.token = os.environ.get("ARMY_TOKEN") or config.token
    return config
