"""The CLI's argument surface.

One test here exists because of a real bug: `--config` and `-v` were declared
only on the top-level parser, so `army run --once -v` was rejected — which is
the form people actually type. The obvious fix (share them via a parent parser
applied to the top level *and* the subcommands) is worse: the subparser writes
its own default over whatever the top-level parse captured, so
`army --config x status` runs silently with no config at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from army.cli import build_parser


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--config", "/tmp/a.toml"],
        ["run", "--once", "--config", "/tmp/a.toml"],
        ["approve", "abc123", "--choice", "merge", "--config", "/tmp/a.toml"],
        ["deny", "abc123", "--config", "/tmp/a.toml"],
        ["effects", "--config", "/tmp/a.toml"],
    ],
)
def test_config_reaches_every_subcommand(argv: list[str]) -> None:
    """A supplied --config must never be silently dropped."""
    assert build_parser().parse_args(argv).config == Path("/tmp/a.toml")


def test_verbose_is_accepted_after_the_subcommand() -> None:
    """`army run --once -v` is the form people type."""
    assert build_parser().parse_args(["run", "--once", "-v"]).verbose is True


def test_flags_before_the_subcommand_fail_loudly() -> None:
    """Wrong order is an error, not a silently ignored flag.

    Silently ignoring it is the failure mode this whole arrangement avoids —
    a loop that runs against the wrong state file is worse than one that
    refuses to start.
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--config", "/tmp/a.toml", "status"])


def test_a_subcommand_is_required() -> None:
    """Bare `army` should say what it can do, not do something."""
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_rate_limit_phrases_come_from_the_config(tmp_path: Path) -> None:
    """Vendor wording differs and changes, so the list has to be settable.

    A phrase that never matches fails silently — the loop keeps collecting
    refusals as finished work — which is exactly the failure this whole check
    exists to stop. Compiling the list in would make that unfixable without a
    release.
    """
    from army.config import load_config

    path = tmp_path / "army.toml"
    path.write_text('[rate_limit]\nphrases = ["Out Of Credits", "please slow down"]\n')

    config = load_config(path)

    assert config.limit_phrases == ("out of credits", "please slow down")


def test_the_example_config_documents_what_the_loader_reads(tmp_path: Path) -> None:
    """The shipped example is the only config most people will ever edit.

    A key it names that the loader ignores is worse than an undocumented one:
    it reads as configured behaviour and silently is not.
    """
    from army.config import load_config
    from army.lanes import DEFAULT_LIMIT_PHRASES

    example = Path(__file__).resolve().parents[2] / "docs" / "agent-army" / "army.toml.example"

    config = load_config(example)

    assert config.limit_phrases == DEFAULT_LIMIT_PHRASES
    assert config.lanes
