"""The file a bot is written in, and the rules it has to satisfy to exist.

A bot is pure data — persona, mission, cadence, a workload reference — so it can
be added at runtime without deploying anything. This module is the boundary
where that data becomes trustworthy: it is the only place a YAML file or an API
body turns into a :class:`~army.bots.model.Bot`, so it is the only place that
has to be careful.

Careful means refusing rather than defaulting. A definition missing a mission
gets an error naming the field, not an empty string that produces a bot which
does nothing and looks healthy doing it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from army.bots.model import (
    MAX_DEPTH,
    Bot,
    InvalidWakePolicy,
    WakeKind,
    WakePolicy,
)

#: The definition format's own version, so a file written today can be read by
#: a reader that has since learned new fields. Bumped only when the *meaning*
#: of an existing field changes; adding an optional one does not.
SPEC_VERSION = 1

#: A slug is an address. It appears in message routing (``bot:scout``), in a
#: workspace path, and in a browser partition key, so it may contain nothing
#: that would need escaping in any of the three.
_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")

#: Fields a definition must carry. Everything else has a defensible default;
#: these four do not, because a bot without them is not a bot.
REQUIRED = ("slug", "persona", "mission", "workload")

#: Fields that decide *where* a bot may act, and which a bot-authored
#: definition may therefore not set. Each one is a capability wearing the
#: clothes of configuration:
#:
#: - ``workspace`` becomes the sandbox's only writable path, so ``/`` is a
#:   filesystem;
#: - ``browser_profile`` is a cookie jar, so naming another bot's is taking its
#:   logged-in sessions;
#: - ``docs_ref`` is where reports are pushed.
#:
#: A person writing YAML may set all three — they already have the filesystem.
#: A bot proposing a child may not, and they are derived from its slug instead.
DERIVED_FOR_SPAWNED = ("workspace", "browser_profile", "docs_ref", "source_agent")


class InvalidDefinition(ValueError):
    """Raised when a definition cannot be turned into a bot.

    The message names the field and what was wrong with it, because the person
    reading it is editing a YAML file and wants to know which line.
    """


def load_file(path: str | Path) -> dict[str, Any]:
    """
    Read a definition file.

    :param path: A ``.yaml``, ``.yml`` or ``.json`` file.
    :returns: The parsed mapping.
    :raises InvalidDefinition: If the file is missing or is not a mapping.
    """
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise InvalidDefinition(f"no bot definition at {resolved}")
    try:
        # safe_load, not load: a bot definition may arrive from a bot, and
        # full YAML can construct arbitrary Python objects.
        data = yaml.safe_load(resolved.read_text())
    except yaml.YAMLError as exc:
        raise InvalidDefinition(f"{resolved} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidDefinition(f"{resolved} must contain a mapping, not {type(data).__name__}")
    return data


def to_bot(
    spec: dict[str, Any],
    *,
    created_by: str,
    now: int,
    parent: Bot | None = None,
) -> Bot:
    """
    Turn a definition into a bot, in ``DRAFT``.

    Always draft. Activation is a separate, human move — that is what makes
    runaway self-replication structurally impossible rather than merely
    discouraged, and it applies to a definition a person wrote just as much as
    to one a bot proposed.

    :param spec: The parsed definition.
    :param created_by: ``"human:<id>"`` or ``"bot:<id>"``.
    :param now: Epoch seconds.
    :param parent: The bot proposing this one, when there is one. Supplies
        lineage and enforces the depth cap.
    :returns: The bot, not yet persisted.
    :raises InvalidDefinition: If the definition is incomplete or incoherent.
    """
    version = spec.get("spec_version", SPEC_VERSION)
    if not isinstance(version, int) or version > SPEC_VERSION:
        raise InvalidDefinition(
            f"spec_version {version!r} is newer than this build understands "
            f"(supports up to {SPEC_VERSION})"
        )

    missing = [field for field in REQUIRED if not str(spec.get(field, "")).strip()]
    if missing:
        raise InvalidDefinition(f"definition is missing required field(s): {', '.join(missing)}")

    slug = str(spec["slug"]).strip()
    if not _SLUG.match(slug):
        raise InvalidDefinition(
            f"slug {slug!r} must be lowercase letters, digits and hyphens, 3–32 characters, "
            "starting with a letter — it is used as an address, a path and a partition key"
        )

    wake = _wake_policy(spec)
    depth = 0 if parent is None else parent.depth + 1
    if depth > MAX_DEPTH:
        raise InvalidDefinition(
            f"{slug!r} would sit at depth {depth}; the cap is {MAX_DEPTH}. "
            "A child may not create grandchildren."
        )

    workload_config = spec.get("workload_config") or {}
    if not isinstance(workload_config, dict):
        raise InvalidDefinition("'workload_config' must be a mapping")

    if parent is not None:
        _refuse_privileged_fields(spec, slug=slug, parent=parent)
        workload_config = _inherited_config(workload_config, parent, slug=slug)

    return Bot.new(
        slug,
        display_name=str(spec.get("display_name") or slug),
        persona=str(spec["persona"]).strip(),
        mission=str(spec["mission"]).strip(),
        workload=str(spec["workload"]).strip(),
        wake=wake,
        created_by=created_by,
        now=now,
        title=_optional_text(spec, "title"),
        workload_config=workload_config,
        # A child runs on its parent's vendor. Letting it pick another lane
        # would let a bot whose lane is cooling spawn its way onto a fresh one.
        harness=parent.harness if parent is not None else _optional_text(spec, "harness"),
        workspace=None if parent is not None else _optional_text(spec, "workspace"),
        browser_profile=(
            f"persist:bot-{slug}"
            if parent is not None
            else (_optional_text(spec, "browser_profile") or f"persist:bot-{slug}")
        ),
        docs_ref=None if parent is not None else _optional_text(spec, "docs_ref"),
        # A bot another bot invented is not an instance of anybody's roster
        # row. Letting a child claim one would let a spawned bot inherit an
        # authority the roster never granted it.
        source_agent=None if parent is not None else _optional_text(spec, "source_agent"),
        expires_at=_expiry(spec, now=now),
        parent_bot_id=parent.id if parent else None,
        root_bot_id=(parent.root_bot_id or parent.id) if parent else None,
        depth=depth,
    )


def _refuse_privileged_fields(spec: dict[str, Any], *, slug: str, parent: Bot) -> None:
    """
    Refuse a bot-authored definition that names where it may act.

    Loudly, rather than by silently dropping the field. A bot that tried is
    doing something worth an operator seeing — and one that merely copied a
    template needs to be told why it was rejected.

    :param spec: The proposed definition.
    :param slug: The proposed name, for the message.
    :param parent: The proposing bot.
    :raises InvalidDefinition: If any privileged field is present.
    """
    named = [field for field in DERIVED_FOR_SPAWNED if spec.get(field)]
    if named:
        raise InvalidDefinition(
            f"{parent.slug} proposed {slug!r} with {', '.join(named)} set. "
            "Those decide where a bot may write and whose logged-in sessions it "
            "uses, so a bot may not choose them for its child — they are derived "
            "from the slug. Remove them."
        )


def _inherited_config(proposed: dict[str, Any], parent: Bot, *, slug: str) -> dict[str, Any]:
    """
    Let a child be configured only where its parent already is.

    ``workload_config`` reaches a workload's constructor as keyword arguments,
    so an unconstrained one is an arbitrary call into operator code — a queue
    path pointed at ``~/.ssh/authorized_keys``, say. A bot cannot hand its child
    a key it does not itself hold.

    :param proposed: What the parent asked for.
    :param parent: The proposing bot.
    :param slug: The proposed name, for the message.
    :returns: The config, restricted to keys the parent already has.
    :raises InvalidDefinition: If it asks for a key the parent does not have.
    """
    unknown = sorted(set(proposed) - set(parent.workload_config))
    if unknown:
        raise InvalidDefinition(
            f"{parent.slug} proposed {slug!r} with workload_config keys it does not "
            f"itself have: {', '.join(unknown)}. Those become constructor arguments, "
            "so a bot may only pass on configuration it was given. Ask a person to "
            "write this one."
        )
    return dict(proposed)


def _wake_policy(spec: dict[str, Any]) -> WakePolicy:
    """
    Build the wake policy, translating the error into the file's vocabulary.

    :param spec: The definition.
    :returns: The policy.
    :raises InvalidDefinition: If the wake block is unusable.
    """
    raw = spec.get("wake")
    if raw is None:
        raise InvalidDefinition(
            "definition needs a 'wake' block; a bot with no cadence would never run. "
            f"Choose one of: {', '.join(k.value for k in WakeKind)}"
        )
    if not isinstance(raw, dict):
        raise InvalidDefinition("'wake' must be a mapping, e.g. {kind: rrule, rrule: FREQ=DAILY}")
    try:
        return WakePolicy.from_dict(raw)
    except InvalidWakePolicy as exc:
        raise InvalidDefinition(f"wake policy: {exc}") from exc


def _optional_text(spec: dict[str, Any], field: str) -> str | None:
    """Read a field that may be absent, blank or null, as ``None`` or text."""
    value = spec.get(field)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _expiry(spec: dict[str, Any], *, now: int) -> int | None:
    """
    Resolve a TTL into an absolute expiry.

    Relative in the file and absolute in the row: ``expires_in_s: 3600`` is
    what a person writes, and an epoch is what survives a restart without
    quietly extending itself every time the process starts.

    :param spec: The definition.
    :param now: Epoch seconds.
    :returns: Epoch seconds, or ``None`` for a bot that does not expire.
    :raises InvalidDefinition: If both forms are given, or the value is not a
        positive integer.
    """
    relative = spec.get("expires_in_s")
    absolute = spec.get("expires_at")
    if relative is not None and absolute is not None:
        raise InvalidDefinition("give 'expires_in_s' or 'expires_at', not both")
    if relative is None and absolute is None:
        return None
    try:
        if relative is not None:
            seconds = int(relative)
            if seconds <= 0:
                raise ValueError
            return now + seconds
        return int(absolute)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise InvalidDefinition("expiry must be a positive whole number of seconds") from exc


def to_yaml(bot: Bot) -> str:
    """
    Render a bot back to a definition file.

    Round-trips: what this writes, :func:`to_bot` reads. That is what lets a
    bot invented at runtime be exported, reviewed in a pull request, and
    committed next to the ones a person wrote.

    :param bot: The bot.
    :returns: YAML text.
    """
    spec: dict[str, Any] = {
        "spec_version": SPEC_VERSION,
        "slug": bot.slug,
        "display_name": bot.display_name,
        "persona": bot.persona,
        "mission": bot.mission,
        "workload": bot.workload,
        "wake": bot.wake.to_dict(),
    }
    for field, value in (
        ("title", bot.title),
        ("harness", bot.harness),
        ("workspace", bot.workspace),
        ("browser_profile", bot.browser_profile),
        ("docs_ref", bot.docs_ref),
        ("source_agent", bot.source_agent),
        ("expires_at", bot.expires_at),
    ):
        if value:
            spec[field] = value
    if bot.workload_config:
        spec["workload_config"] = bot.workload_config
    return str(yaml.safe_dump(spec, sort_keys=False, default_flow_style=False))


#: A definition that works out of the box, printed by ``army bots example``.
#: Deliberately the heartbeat: someone trying Bot mode for the first time
#: should see the loop run before they have configured a vendor.
EXAMPLE = """\
spec_version: 1
slug: heartbeat
display_name: Heartbeat
title: Nothing In Particular
persona: >-
  You are a heartbeat. You exist to prove the loop runs.
mission: >-
  Complete an iteration, report that work was done, and wait to be asked again.
workload: army.bots.workloads.heartbeat:HeartbeatWorkload
workload_config:
  outcome: no_work
wake:
  kind: continuous
  min_interval_s: 30
  precondition: always
  backoff:
    base_s: 30
    max_s: 300
    factor: 2
"""
