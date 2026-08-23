"""The import boundary, enforced rather than documented.

``army/bots/`` may import ``army``; nothing in ``army`` may import
``army.bots``. That direction is what keeps the run state machine
comprehensible on its own, and it is what makes deleting the whole of Bot mode
a one-command operation if it ever stops earning its keep.

A rule written only in a docstring is a rule that lasts until someone is in a
hurry. This is the test that notices.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from army.bots.store import BotStore
from tests.army.bots.conftest import activate, make_bot

NOW = 1_700_000_000
HEARTBEAT = "army.bots.workloads.heartbeat:HeartbeatWorkload"

ARMY = Path(__file__).resolve().parents[3] / "army"

#: The single sanctioned crossing. ``army/cli.py`` grows one subcommand that
#: lazy-imports the bot CLI *inside the function body*, so importing ``army``
#: never pulls in Bot mode and the boundary holds at module scope.
_SANCTIONED_LAZY_IMPORT = "cli.py"


def _core_modules() -> list[Path]:
    """Every module in ``army/`` that is not part of Bot mode."""
    return sorted(
        path
        for path in ARMY.rglob("*.py")
        if "bots" not in path.relative_to(ARMY).parts and "__pycache__" not in str(path)
    )


def test_core_modules_exist() -> None:
    """Guard the guard: a glob that matches nothing would pass every test below."""
    names = {path.name for path in _core_modules()}
    assert {"store.py", "supervisor.py", "state.py", "workload.py"} <= names


@pytest.mark.parametrize("module", _core_modules(), ids=lambda p: p.name)
def test_core_never_imports_bot_mode_at_module_scope(module: Path) -> None:
    """``army`` must be importable and usable with ``army/bots/`` deleted."""
    tree = ast.parse(module.read_text(), filename=str(module))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        if not any(name == "army.bots" or name.startswith("army.bots.") for name in names):
            continue
        if module.name == _SANCTIONED_LAZY_IMPORT and node.col_offset > 0:
            # Indented, so it is inside a function: the lazy crossing.
            continue
        offenders.append(f"line {node.lineno}: {names}")
    assert not offenders, (
        f"{module.relative_to(ARMY.parent)} imports Bot mode at module scope: {offenders}. "
        "The arrow points army/bots -> army, never back."
    )


def test_deleting_bot_mode_would_not_break_the_core_suite() -> None:
    """No core test may reference Bot mode either, or the suites are entangled."""
    core_tests = Path(__file__).resolve().parents[1]
    offenders = [path.name for path in core_tests.glob("*.py") if "army.bots" in path.read_text()]
    assert not offenders, f"core tests reference Bot mode: {offenders}"


def test_bot_tables_are_absent_until_bot_mode_opens_the_store(tmp_path: Path) -> None:
    """A plain ``Store`` must not create bot tables.

    Existing deployments that never enable Bot mode should keep a four-table
    database, so ``army`` remains as small as its own docs claim.
    """
    from army.store import Store

    store = Store(tmp_path / "army.db")
    with store.atomic() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "bots" not in tables
    assert "bot_revisions" not in tables
    assert {"runs", "commands", "effects", "used_grants"} <= tables


def test_the_runs_columns_do_belong_to_the_core_store(tmp_path: Path) -> None:
    """But the three ``runs`` columns and the index are core's business.

    They are on a table ``army`` owns, and the partial unique index is what
    protects it. Installing them from Bot mode would leave a database where
    ``runs`` has a ``bot_id`` nobody is enforcing uniqueness on.
    """
    from army.store import Store

    store = Store(tmp_path / "army.db")
    with store.atomic() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    assert {"bot_id", "revision_id", "outcome"} <= columns
    assert "one_live_run_per_bot" in indexes


# ── isolation that is delivered or refused ────────────────────────


def test_a_worktree_that_cannot_be_made_refuses_rather_than_sharing(
    bots: BotStore, tmp_path: Path
) -> None:
    """
    The failure mode this replaces is the dangerous one.

    `_add_worktree` used to log a warning and carry on, so a bot that asked for
    an isolated checkout silently got the operator's working tree — and then
    committed into it. A log line nobody reads is not a control.
    """
    from army.bots.workspace import Workspace, WorkspaceRefused

    bot = activate(bots, make_bot("researcher", workload=HEARTBEAT), now=NOW)
    workspace = Workspace(bots, root=tmp_path / "bots")
    # Not a repository, so `git worktree add` cannot succeed.
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    with pytest.raises(WorkspaceRefused, match="Refusing rather than sharing"):
        workspace.prepare(bot, now=NOW, source_repo=not_a_repo)


def test_every_run_records_what_it_was_allowed_to_touch(bots: BotStore, tmp_path: Path) -> None:
    """
    `profile_for` existed and nothing called it, which made the isolation story
    a design rather than a control — and an uncalled security helper reads
    exactly like an enforced one to anyone skimming.
    """
    from army.bots.isolation import sandbox_for

    bot = make_bot("researcher", workload=HEARTBEAT)
    bot.workspace = str(tmp_path / "ws")
    profile = sandbox_for(bot)

    assert profile["write_paths"] == [str(tmp_path / "ws")]
    # The vendor logins and the control plane are withheld: a bot that can read
    # either does not need the lane accounting or the approval it was given.
    denied = " ".join(profile["deny_read_paths"])  # type: ignore[arg-type]
    assert ".claude" in denied and ".omnigent/army" in denied


def test_the_profile_says_what_does_not_enforce_it(bots: BotStore, tmp_path: Path) -> None:
    """
    Recording is not enforcing.

    Omnigent's own file tools respect an environment root; the vendor CLIs'
    native tools do not, and the trading-army repository has a run on record
    that was handed `/tmp` and read a different repository anyway. Writing
    `sandboxed: true` here would be the lie that run disproves.
    """
    from army.bots.isolation import sandbox_for

    bot = make_bot("researcher", workload=HEARTBEAT)
    bot.workspace = str(tmp_path / "ws")
    enforced = sandbox_for(bot)["enforced_by"]

    assert enforced["native_vendor_tools"].startswith("nothing")  # type: ignore[index]
    assert enforced["process"].startswith("none")  # type: ignore[index]
