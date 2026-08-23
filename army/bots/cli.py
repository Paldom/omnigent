"""``army bots`` — define a bot, activate it, see what the fleet is doing.

The operator's whole vocabulary is five verbs: write one, turn it on, look at
it, wake it, turn it off. Everything else the fleet does to itself.

``status`` is the one that earns its place. It answers herdr's question —
*which one is stuck?* — from derived state, so it can never claim a bot is
scheduled while its run sits waiting on a person.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

from army.bots.approvals import ApprovalRefused, ApprovalStore, owner_broker
from army.bots.definition import EXAMPLE, InvalidDefinition, load_file, to_bot, to_yaml
from army.bots.messages import MessageKind, MessageStore
from army.bots.model import Bot, BotStatus, DerivedStatus, IllegalBotMove
from army.bots.precondition import PreconditionRegistry
from army.bots.registry import WorkloadRegistry
from army.bots.roster import RosterEntry, roster, summarise
from army.bots.schedule import first_wake
from army.bots.store import BotStore
from army.bots.supervisor import BotSupervisor, wake_now
from army.bots.web import BotsSite, serve
from army.config import Config
from army.gates import GateRefused, Grant
from army.lanes import Lanes
from army.omni import OmniClient
from army.store import ConcurrentTransition, Store

_logger = logging.getLogger("army.bots")

#: How a derived status reads in a terminal. One word each, because the column
#: is scanned rather than read, and a marker only where a person is needed.
_MARKER: dict[DerivedStatus, str] = {
    DerivedStatus.WAITING_HUMAN: "<- waiting on you",
    DerivedStatus.BLOCKED: "<- stuck, nothing will wake it",
}


def _open(config: Config) -> tuple[Store, BotStore]:
    """Open the shared database and the bot tables over it."""
    store = Store(config.state_path)
    return store, BotStore(store)


def _channel(config: Config) -> tuple[BotStore, MessageStore, ApprovalStore]:
    """Open the bot tables, the channel and the approval ledger."""
    _, bots = _open(config)
    return bots, MessageStore(bots), ApprovalStore(bots, owner_broker(bots))


def _fleet(config: Config) -> tuple[Store, BotStore, BotSupervisor]:
    """Wire a supervisor that can drive the whole roster."""
    store, bots = _open(config)
    lanes = (
        Lanes.from_config(config.lanes, limit_phrases=config.limit_phrases)
        if config.lanes
        else None
    )
    supervisor = BotSupervisor(
        store,
        OmniClient(config.server_url, token=config.token),
        bots,
        WorkloadRegistry(allow=config.allow_workloads),
        preconditions=PreconditionRegistry(),
        lanes=lanes,
        max_concurrent_runs=config.max_concurrent_runs,
        messages=MessageStore(bots),
        approvals=ApprovalStore(bots, owner_broker(bots)),
    )
    return store, bots, supervisor


def cmd_pending(config: Config, args: argparse.Namespace) -> int:
    """
    Show every question waiting on a person.

    :param config: Resolved configuration.
    :param args: Uses ``--bot``.
    :returns: Process exit code.
    """
    bots, _, approvals = _channel(config)
    outstanding = approvals.pending(bot_id=_bot_id(bots, args.bot) if args.bot else None)
    if not outstanding:
        print("nothing is waiting on you")
        return 0
    now = int(time.time())
    for request in outstanding:
        bot = bots.get(request.bot_id)
        owner = " OWNER-ONLY" if request.requires_owner else ""
        left = ""
        if request.expires_at is not None:
            left = f"expires in {_short(request.expires_at - now)}"
        print(f"{request.id[:12]}  {bot.slug if bot else request.bot_id:<16}{left:>18}{owner}")
        print(f"{'':14}{request.question}")
        if request.options:
            print(f"{'':14}options: {', '.join(request.options)}")
        print(f"{'':14}army bots approve {request.id[:12]} --choice <option>")
    return 0


def cmd_verdict(config: Config, args: argparse.Namespace) -> int:
    """
    Answer a question, bound to the operation it was asked about.

    :param config: Resolved configuration.
    :param args: Uses ``approval``, ``--choice`` and ``--grant``.
    :returns: Process exit code.
    """
    _, messages, approvals = _channel(config)
    request = approvals.get(args.approval)
    if request is None:
        print(f"no approval matching {args.approval!r}", file=sys.stderr)
        return 1

    grant = None
    if getattr(args, "grant", None):
        try:
            grant = Grant.from_token(args.grant)
        except GateRefused as exc:
            print(f"{exc}", file=sys.stderr)
            return 1

    approved = args.command == "approve"
    now = int(time.time())
    run = Store(config.state_path).get_run(request.run_id)
    try:
        command = approvals.decide(
            request,
            approved=approved,
            decided_by=f"human:{_whoami()}",
            now=now,
            choice=getattr(args, "choice", None),
            # Re-checked against the run as it stands now, so a verdict cannot
            # be applied to an iteration that moved on while it sat waiting.
            run_version=run.version if run is not None else None,
            grant=grant,
        )
    except (ApprovalRefused, ConcurrentTransition) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    messages.post(
        request.bot_id,
        f"human:{_whoami()}",
        MessageKind.VERDICT,
        f"{'Approved' if approved else 'Denied'}"
        + (f": {args.choice}" if getattr(args, "choice", None) else ""),
        now=now,
        payload={"approval_id": request.id, "approved": approved},
        thread_id=request.thread_id,
        run_id=request.run_id,
        command_id=command.id,
    )
    print(f"recorded {command.kind.value} for {request.id[:12]}; the loop applies it next tick")
    return 0


def cmd_channel(config: Config, args: argparse.Namespace) -> int:
    """
    Read a bot's channel.

    :param config: Resolved configuration.
    :param args: Uses ``bot``, ``--after`` and ``--limit``.
    :returns: Process exit code.
    """
    bots, messages, _ = _channel(config)
    bot = _resolve(bots, args.bot)
    if bot is None:
        return 1
    rows = messages.channel(bot.id, after_seq=args.after, limit=args.limit)
    if not rows:
        print(f"{bot.slug}'s channel is empty")
        return 0
    for message in rows:
        thread = f" [{message.thread_id[:8]}]" if message.thread_id else ""
        print(f"{message.seq:>5}  {message.kind.value:<12}{message.author:<20}{thread}")
        for line in message.body.splitlines() or [""]:
            print(f"{'':7}{line}")
    return 0


def _bot_id(bots: BotStore, name: str) -> str | None:
    """Resolve a slug to an id for a filter, without complaining when absent."""
    bot = bots.by_slug(name) or bots.get(name)
    return bot.id if bot else None


def cmd_create(config: Config, args: argparse.Namespace) -> int:
    """
    Define a bot from a YAML file, in ``DRAFT``.

    Draft rather than active, always. Reading a file is not the same as
    deciding to run what is in it.

    :param config: Resolved configuration.
    :param args: Uses ``file`` and ``--activate``.
    :returns: Process exit code.
    """
    _, bots = _open(config)
    try:
        spec = load_file(args.file)
        bot = to_bot(spec, created_by=f"human:{_whoami()}", now=int(time.time()))
    except InvalidDefinition as exc:
        print(f"{args.file}: {exc}", file=sys.stderr)
        return 1

    registry = WorkloadRegistry(allow=config.allow_workloads)
    if not registry.permits(bot.workload):
        print(
            f"{bot.slug}: workload {bot.workload!r} is not allowed here.\n"
            "  Add it to [bots] allow_workloads in army.toml, or use one that ships "
            "with the loop.",
            file=sys.stderr,
        )
        return 1

    try:
        bots.create(bot)
    except ConcurrentTransition as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    print(f"created {bot.slug} ({bot.id[:12]}) as draft, revision 1")

    if args.activate:
        return _activate(bots, bot)
    print(f"  army bots activate {bot.slug}")
    return 0


def cmd_activate(config: Config, args: argparse.Namespace) -> int:
    """
    Turn a bot on, and give it its first wake in the same write.

    :param config: Resolved configuration.
    :param args: Uses ``bot``.
    :returns: Process exit code.
    """
    _, bots = _open(config)
    bot = _resolve(bots, args.bot)
    if bot is None:
        return 1
    return _activate(bots, bot)


def _activate(bots: BotStore, bot: Bot) -> int:
    """Move a bot to ``ACTIVE`` with its first wake, reporting what happens next."""
    now = int(time.time())
    try:
        bots.set_status(bot, BotStatus.ACTIVE, now=now, wake=first_wake(bot.wake, now=now))
    except (IllegalBotMove, ConcurrentTransition) as exc:
        print(f"{bot.slug}: {exc}", file=sys.stderr)
        return 1
    when = "now"
    if bot.next_due_at is not None and bot.next_due_at > now:
        when = _in(bot.next_due_at, now)
    if bot.next_due_at is None:
        when = f"when something wakes it ({bot.wake.kind.value})"
    print(f"activated {bot.slug}; first wake {when}")
    return 0


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    """
    Show the fleet, ordered so what needs a person is at the top.

    :param config: Resolved configuration.
    :param args: Uses ``--all``.
    :returns: Process exit code.
    """
    _, bots = _open(config)
    now = int(time.time())
    entries = roster(bots, now=now)
    if not entries:
        print("no bots yet")
        print("  army bots example > heartbeat.yaml && army bots create heartbeat.yaml --activate")
        return 0

    shown = entries if args.all else [e for e in entries if e.status is not DerivedStatus.INACTIVE]
    counts = summarise(entries)
    print(" · ".join(f"{count} {status}" for status, count in sorted(counts.items())))
    print()
    for entry in shown:
        print(_line(entry, now))
        detail = _detail(entry, now)
        if detail:
            print(f"{'':16}{detail}")

    hidden = len(entries) - len(shown)
    if hidden:
        print(f"\n{hidden} not active (--all to show)")
    return 0


def _line(entry: RosterEntry, now: int) -> str:
    """One roster row: name, what it is doing, when it next wakes."""
    delay = entry.due_in(now)
    when = "—" if delay is None else ("due" if delay == 0 else _short(delay))
    marker = _MARKER.get(entry.status, "")
    return f"{entry.bot.slug:<16}{entry.status.value:<18}{when:>8}  {marker}"


def _detail(entry: RosterEntry, now: int) -> str:
    """The reason line under a row, when there is something worth saying."""
    bot = entry.bot
    if entry.status is DerivedStatus.WAITING_HUMAN and entry.live_run_id:
        return f"army approve {entry.live_run_id[:12]}"
    if entry.status is DerivedStatus.BLOCKED:
        return "no next wake and nothing pending — army bots wake " + bot.slug
    if entry.status is DerivedStatus.WAITING_RESOURCE and entry.blocked_until:
        return f"{bot.harness} lane cooling for {_short(entry.blocked_until - now)}"
    if entry.status is DerivedStatus.BACKING_OFF:
        return f"nothing to do {bot.idle_streak}x running"
    if entry.status is DerivedStatus.RUNNING and entry.live_run_id:
        return f"run {entry.live_run_id[:12]}"
    return ""


def cmd_show(config: Config, args: argparse.Namespace) -> int:
    """
    Print one bot's definition and its recent iterations.

    :param config: Resolved configuration.
    :param args: Uses ``bot``.
    :returns: Process exit code.
    """
    _, bots = _open(config)
    bot = _resolve(bots, args.bot)
    if bot is None:
        return 1
    print(to_yaml(bot))
    revisions = bots.revisions(bot.id)
    print(f"revisions: {len(revisions)} (running rev {_rev_of(revisions, bot)})")
    runs = bots.runs_for(bot.id, limit=args.limit)
    if not runs:
        print("no iterations yet")
        return 0
    print()
    for run in runs:
        outcome = run["outcome"] or "—"
        print(
            f"{run['id'][:12]}  {run['state']:<14}{outcome:<16}"
            f"{_ago(run['updated_at']):>10}  {run['terminal_reason'] or ''}"
        )
    return 0


def cmd_wake(config: Config, args: argparse.Namespace) -> int:
    """
    Make a bot due immediately.

    The manual half of the insert-driven wake model, and the recovery for a
    succession that got lost.

    :param config: Resolved configuration.
    :param args: Uses ``bot``.
    :returns: Process exit code.
    """
    _, bots = _open(config)
    bot = _resolve(bots, args.bot)
    if bot is None:
        return 1
    if bot.status is not BotStatus.ACTIVE:
        print(f"{bot.slug} is {bot.status.value}, not active", file=sys.stderr)
        return 1
    wake_now(bots, bot, now=int(time.time()))
    print(f"{bot.slug} is due now; the loop picks it up on its next tick")
    return 0


def cmd_lifecycle(config: Config, args: argparse.Namespace) -> int:
    """
    Pause, resume or retire a bot.

    :param config: Resolved configuration.
    :param args: Uses ``bot`` and the subcommand name.
    :returns: Process exit code.
    """
    _, bots = _open(config)
    bot = _resolve(bots, args.bot)
    if bot is None:
        return 1
    target = {
        "pause": BotStatus.PAUSED,
        "resume": BotStatus.ACTIVE,
        "retire": BotStatus.RETIRED,
    }[args.command]
    now = int(time.time())
    wake = first_wake(bot.wake, now=now) if target is BotStatus.ACTIVE else None
    try:
        bots.set_status(bot, target, now=now, wake=wake)
    except (IllegalBotMove, ConcurrentTransition) as exc:
        print(f"{bot.slug}: {exc}", file=sys.stderr)
        return 1
    print(f"{bot.slug} is now {target.value}")
    if target is BotStatus.RETIRED:
        children = [child.slug for child in bots.children(bot.id)]
        if children:
            print(f"  its children are still running: {', '.join(children)}")
    return 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    """
    Drive the fleet until interrupted.

    :param config: Resolved configuration.
    :param args: Uses ``--once`` and ``--interval``.
    :returns: Process exit code.
    """
    _, _, supervisor = _fleet(config)
    if not args.offline and not supervisor.omni.health():
        print(
            f"omnigent server at {config.server_url} is not answering.\n"
            "  --offline drives bots whose workloads need no sessions.",
            file=sys.stderr,
        )
        return 1
    _logger.info("army bots: driving the fleet every %ss", args.interval)
    while True:
        report = supervisor.fleet_tick()
        if report.started or report.advanced or report.failed:
            _logger.info("tick: %s", report)
        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            _logger.info("army bots: stopping")
            return 0


def cmd_serve(config: Config, args: argparse.Namespace) -> int:
    """
    Serve the Bots page from this process.

    :param config: Resolved configuration.
    :param args: Uses ``--host`` and ``--port``.
    :returns: Process exit code.
    """
    store = Store(config.state_path)
    bots = BotStore(store)
    site = BotsSite(store, bots, ApprovalStore(bots, owner_broker(bots)), MessageStore(bots))
    server = serve(site, host=args.host, port=args.port)
    print(f"bots page on http://{args.host}:{args.port}/bots  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


def cmd_example(_config: Config, _args: argparse.Namespace) -> int:
    """Print a definition that works without a vendor configured."""
    print(EXAMPLE, end="")
    return 0


def _resolve(bots: BotStore, name: str) -> Bot | None:
    """
    Find a bot by slug or id prefix, complaining usefully when there is none.

    :param bots: The bot store.
    :param name: Slug or leading characters of an id.
    :returns: The bot, or ``None``.
    """
    found = bots.by_slug(name) or bots.get(name)
    if found is not None:
        return found
    matches = [bot for bot in bots.list() if bot.id.startswith(name)]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(bot.slug for bot in bots.list()) or "none"
    print(f"no bot matching {name!r}; this fleet has: {known}", file=sys.stderr)
    return None


def _rev_of(revisions: list[Any], bot: Bot) -> int | str:
    """The revision number a bot's runs currently pin."""
    for revision in revisions:
        if revision.id == bot.current_revision_id:
            return int(revision.rev)
    return "?"


def _whoami() -> str:
    """Who is making a change, for the audit trail."""
    import getpass

    try:
        return getpass.getuser()
    except (OSError, KeyError):
        # No password-database entry, which happens in a container. The audit
        # trail is better with a placeholder than with a crash.
        return "unknown"


def _short(seconds: int) -> str:
    """Render a delay in the least noisy unit that is still honest."""
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _in(when: int, now: int) -> str:
    """Render an absolute time as a delay from now."""
    return f"in {_short(when - now)}"


def _ago(stamp: int) -> str:
    """Render a timestamp as a short relative age."""
    return f"{_short(int(time.time()) - stamp)} ago"


def add_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    """
    Attach ``army bots`` to the main parser.

    Called from :mod:`army.cli` inside the function that handles the
    subcommand, so importing ``army`` never pulls in Bot mode and deleting
    ``army/bots/`` leaves the rest working.

    :param sub: The main parser's subcommand action.
    :param common: Parent parser carrying ``--config`` and ``-v``.
    """
    # `common` goes on the leaves only, never on this group parser. Putting it
    # on both means the leaf's own default overwrites whatever the group
    # captured, so `army bots --config x create f.yaml` silently runs against
    # the default database — which is the trap `army/cli.py` documents, and
    # which this code walked into once already.
    bots = sub.add_parser("bots", help="define and drive long-running bots")
    inner = bots.add_subparsers(dest="command", required=True)

    create = inner.add_parser("create", help="define a bot from a YAML file", parents=[common])
    create.add_argument("file", type=Path, help="path to a bot definition")
    create.add_argument("--activate", action="store_true", help="turn it on straight away")
    create.set_defaults(func=cmd_create)

    activate = inner.add_parser("activate", help="turn a bot on", parents=[common])
    activate.add_argument("bot", help="slug or id prefix")
    activate.set_defaults(func=cmd_activate)

    status = inner.add_parser("status", help="what the fleet is doing", parents=[common])
    status.add_argument("--all", action="store_true", help="include draft, paused and retired")
    status.set_defaults(func=cmd_status)

    show = inner.add_parser("show", help="one bot's definition and iterations", parents=[common])
    show.add_argument("bot", help="slug or id prefix")
    show.add_argument("--limit", type=int, default=10)
    show.set_defaults(func=cmd_show)

    wake = inner.add_parser("wake", help="make a bot due now", parents=[common])
    wake.add_argument("bot", help="slug or id prefix")
    wake.set_defaults(func=cmd_wake)

    for name, help_text in (
        ("pause", "stop scheduling a bot"),
        ("resume", "schedule it again"),
        ("retire", "stop it for good"),
    ):
        action = inner.add_parser(name, help=help_text, parents=[common])
        action.add_argument("bot", help="slug or id prefix")
        action.set_defaults(func=cmd_lifecycle)

    run = inner.add_parser("run", help="drive the fleet", parents=[common])
    run.add_argument("--interval", type=float, default=10.0, help="seconds between ticks")
    run.add_argument("--once", action="store_true", help="tick once and exit")
    run.add_argument(
        "--offline",
        action="store_true",
        help="do not require an Omnigent server, for workloads that need no sessions",
    )
    run.set_defaults(func=cmd_run)

    example = inner.add_parser("example", help="print a working definition", parents=[common])
    example.set_defaults(func=cmd_example)

    pending = inner.add_parser("pending", help="questions waiting on you", parents=[common])
    pending.add_argument("--bot", help="restrict to one bot")
    pending.set_defaults(func=cmd_pending)

    for name, help_text in (("approve", "answer yes"), ("deny", "answer no")):
        verdict = inner.add_parser(name, help=help_text, parents=[common])
        verdict.add_argument("approval", help="approval id or prefix")
        verdict.add_argument("--choice", help="which option, for a multi-option question")
        verdict.add_argument(
            "--grant",
            help="a signed owner grant token, required for spend / execute_order / "
            "add_dependency — a channel verdict alone never satisfies those",
        )
        verdict.set_defaults(func=cmd_verdict)

    site = inner.add_parser("serve", help="serve the Bots page", parents=[common])
    site.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to bind; use the tailnet address to reach it from a phone",
    )
    site.add_argument("--port", type=int, default=6768)
    site.set_defaults(func=cmd_serve)

    channel = inner.add_parser("channel", help="read a bot's channel", parents=[common])
    channel.add_argument("bot", help="slug or id prefix")
    channel.add_argument("--after", type=int, default=0, help="resume from a sequence number")
    channel.add_argument("--limit", type=int, default=50)
    channel.set_defaults(func=cmd_channel)
