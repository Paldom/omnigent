"""``army`` — start the loop, see where it is, answer it.

Three things you actually do with a running orchestrator: leave it running,
find out what it is waiting for, and tell it. Everything else is Omnigent's
job and has a better interface for it already.

    army run                 # drive the loop until stopped
    army status              # what is in flight, what is parked
    army approve <run>       # answer a parked iteration
    army deny <run>
    army effects             # side effects whose outcome a crash left unknown
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from army.config import Config, load_config
from army.lanes import Lanes
from army.omni import OmniClient
from army.state import CommandKind, RunState
from army.store import Store
from army.supervisor import Supervisor

_logger = logging.getLogger("army")


def _build(config: Config) -> tuple[Store, Supervisor]:
    """
    Wire a store, a client and a supervisor from configuration.

    :param config: Resolved configuration.
    :returns: ``(store, supervisor)``.
    """
    store = Store(config.state_path)
    omni = OmniClient(config.server_url, token=config.token)
    lanes = (
        Lanes.from_config(config.lanes, limit_phrases=config.limit_phrases)
        if config.lanes
        else None
    )
    workload = config.load_workload()
    supervisor = Supervisor(
        store,
        omni,
        workload,
        lanes=lanes,
        max_concurrent_runs=config.max_concurrent_runs,
    )
    return store, supervisor


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    """
    Drive the loop until interrupted.

    The tick is the unit of durability: whatever happens between two ticks —
    including the machine going down — the next one starts from rows, not from
    anything this process was holding.

    :param config: Resolved configuration.
    :param args: Parsed arguments; uses ``--once`` and ``--interval``.
    :returns: Process exit code.
    """
    store, supervisor = _build(config)
    if not supervisor.omni.health():
        print(f"omnigent server at {config.server_url} is not answering", file=sys.stderr)
        return 1
    _logger.info("army: driving %s every %ss", config.workload, args.interval)
    while True:
        report = supervisor.tick()
        if report.started or report.advanced or report.failed:
            _logger.info("tick: %s", report)
        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            _logger.info("army: stopping; %d run(s) left in flight", len(store.active_runs()))
            return 0


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    """
    Print what the loop is doing and what it needs from you.

    :param config: Resolved configuration.
    :param args: Parsed arguments; uses ``--limit``.
    :returns: Process exit code.
    """
    store = Store(config.state_path)
    runs = store.list_runs(limit=args.limit)
    if not runs:
        print("no runs yet")
        return 0

    waiting = [run for run in runs if run.state is RunState.WAITING_HUMAN]
    for run in runs:
        marker = "  <- waiting on you" if run.state is RunState.WAITING_HUMAN else ""
        age = _ago(run.updated_at)
        print(f"{run.id[:12]}  {run.state.value:<14} v{run.version:<3} {age:>10}{marker}")
        if run.terminal_reason:
            print(f"{'':14}{run.terminal_reason}")
        if run.state is RunState.PAUSED:
            print(f"{'':14}army resume {run.id[:12]}")

    if waiting:
        print()
        for run in waiting:
            question = run.artifacts.get("question", "(no question recorded)")
            options = run.artifacts.get("options") or []
            print(f"{run.id[:12]}  {question}")
            if options:
                print(f"{'':14}options: {', '.join(str(o) for o in options)}")
            print(f"{'':14}army approve {run.id[:12]} --choice <option>")
    return 0


def cmd_answer(config: Config, args: argparse.Namespace) -> int:
    """
    Record a verdict for a parked run.

    Recording is the whole operation. The supervisor applies it on its next
    tick, so answering while nothing is running is fine and normal.

    :param config: Resolved configuration.
    :param args: Parsed arguments; uses ``run`` and ``--choice``.
    :returns: Process exit code.
    """
    store, supervisor = _build(config)
    run = _resolve_run(store, args.run)
    if run is None:
        print(f"no run matching {args.run!r}", file=sys.stderr)
        return 1
    if run.state is not RunState.WAITING_HUMAN:
        print(f"run {run.id[:12]} is {run.state.value}, not waiting on you", file=sys.stderr)
        return 1

    kind = CommandKind.APPROVE if args.command == "approve" else CommandKind.DENY
    payload = {"choice": args.choice} if getattr(args, "choice", None) else {}
    supervisor.answer(run.id, kind, payload)
    print(f"recorded {kind.value} for {run.id[:12]}; the loop applies it on its next tick")
    return 0


def cmd_resume(config: Config, args: argparse.Namespace) -> int:
    """
    Restart a branch a decline had paused.

    Declining stops a branch on purpose, so nothing restarts it automatically —
    but something has to be able to, or "paused" is just a nicer word for
    abandoned.

    :param config: Resolved configuration.
    :param args: Parsed arguments; uses ``run``.
    :returns: Process exit code.
    """
    store, supervisor = _build(config)
    run = _resolve_run(store, args.run)
    if run is None:
        print(f"no run matching {args.run!r}", file=sys.stderr)
        return 1
    if run.state is not RunState.PAUSED:
        print(f"run {run.id[:12]} is {run.state.value}, not paused", file=sys.stderr)
        return 1
    supervisor.resume(run.id)
    print(f"recorded resume for {run.id[:12]}; the loop restarts it on its next tick")
    return 0


def cmd_effects(config: Config, _args: argparse.Namespace) -> int:
    """
    List side effects whose outcome is unknown.

    A row here means a process claimed an external action and did not live to
    record how it went. Nothing retries one of these on its own — check the
    system it acted on first, because the action may well have happened.

    :param config: Resolved configuration.
    :param _args: Unused; the subcommand takes no options.
    :returns: Process exit code.
    """
    store = Store(config.state_path)
    unresolved = store.unreconciled_effects()
    if not unresolved:
        print("no unreconciled effects")
        return 0
    print("These external actions may or may not have happened. Check, then resolve:\n")
    for effect in unresolved:
        print(f"  {effect['id']}  {effect['kind']}  run={effect['run_id'][:12]}")
        print(f"      claimed {_ago(effect['created_at'])}, request={effect['request']}")
    return 0


def _resolve_run(store: Store, prefix: str) -> object | None:
    """
    Find a run by id or unambiguous id prefix.

    :param store: Where runs live.
    :param prefix: Full id or leading characters of one.
    :returns: The run, or ``None`` when nothing or more than one matches.
    """
    exact = store.get_run(prefix)
    if exact is not None:
        return exact
    matches = [run for run in store.list_runs(limit=500) if run.id.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def _ago(stamp: int) -> str:
    """Render a timestamp as a short relative age."""
    delta = max(0, int(time.time()) - stamp)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def build_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser.

    :returns: The parser.
    """
    # --config and -v live on the subcommands, not above them, so
    # `army run --once -v` works — which is what people type. Sharing them via
    # a parent parser that is ALSO applied to the top level looks tidier and is
    # a trap: the subparser writes its own default over whatever the top-level
    # parse captured, so `army --config x status` silently runs with no config.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="path to army.toml")
    common.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(prog="army", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="drive the loop", parents=[common])
    run.add_argument("--interval", type=float, default=10.0, help="seconds between ticks")
    run.add_argument("--once", action="store_true", help="tick once and exit")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show runs and outstanding questions", parents=[common])
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(func=cmd_status)

    for name, help_text in (("approve", "approve a parked run"), ("deny", "decline it")):
        answer = sub.add_parser(name, help=help_text, parents=[common])
        answer.add_argument("run", help="run id or unambiguous prefix")
        answer.add_argument("--choice", help="which option, for a multi-option question")
        answer.set_defaults(func=cmd_answer)

    resume = sub.add_parser("resume", help="restart a paused run", parents=[common])
    resume.add_argument("run", help="run id or unambiguous prefix")
    resume.set_defaults(func=cmd_resume)

    effects = sub.add_parser(
        "effects", help="list side effects with an unknown outcome", parents=[common]
    )
    effects.set_defaults(func=cmd_effects)

    # The one place `army` knows Bot mode exists, and it is a lazy import
    # inside a function: `import army.cli` must not pull in the fleet, and
    # deleting `army/bots/` must leave everything above still working.
    try:
        from army.bots.cli import add_parser as add_bots_parser
    except ImportError:  # pragma: no cover — only when army/bots/ is removed
        pass
    else:
        add_bots_parser(sub, common)
    return parser


def main(argv: list[str] | None = None) -> int:
    """
    Entry point.

    :param argv: Arguments, or ``None`` to read ``sys.argv``.
    :returns: Process exit code.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config(args.config)
    return int(args.func(config, args))


if __name__ == "__main__":
    raise SystemExit(main())
