"""A bot's own directory, which is also its git checkout and its documentation.

One place, three jobs, solved by convention rather than by a second store:
``~/bots/<slug>/`` is the worktree the bot works in, the repository its charter
and runbook live in, and the folder its reports land in. Git already gives
history, diffs, review and portability; duplicating that in SQLite would be the
wrong kind of work.

``bot_docs`` is therefore an *index*, not a store. The content lives in files.
The table exists so a bot can navigate its own documentation without listing a
directory, and so a report is addressable by the iteration that produced it —
which is the property that makes a report auditable rather than merely filed.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from army.bots.model import Bot
from army.bots.store import BotStore, _statements

_logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_docs (
    id         TEXT PRIMARY KEY,
    bot_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    title      TEXT NOT NULL,
    run_id     TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (bot_id, path)
);
CREATE INDEX IF NOT EXISTS ix_docs_bot ON bot_docs (bot_id, kind, created_at);
"""

#: Where a bot's directory lives when nothing says otherwise.
DEFAULT_ROOT = Path.home() / "bots"

#: Files every bot starts with. The charter is the one that matters: it is the
#: mission in a form a person can edit in a pull request, which is what makes a
#: bot's purpose reviewable rather than buried in a database column.
_SEED = {
    "charter.md": "# {display_name}\n\n{mission}\n\n## Standing role\n\n{persona}\n",
    "runbook.md": (
        "# Runbook — {display_name}\n\n"
        "What to check when this bot misbehaves, and what it is allowed to do.\n\n"
        "## Wake policy\n\n`{wake}`\n\n"
        "## When it is stuck\n\n"
        "- `army bots status` — what it thinks it is doing\n"
        "- `army bots channel {slug}` — what it has been saying\n"
        "- `army bots pending` — whether it is waiting on you\n"
    ),
    "reports/.gitkeep": "",
    "lessons.md": "# Lessons\n\nWhat this bot has learned, newest last.\n",
}


class DocKind(str, Enum):
    """What a document is for."""

    CHARTER = "charter"
    RUNBOOK = "runbook"
    REPORT = "report"
    LESSON = "lesson"


@dataclass(frozen=True)
class Doc:
    """One indexed document.

    :param id: Stable id.
    :param bot_id: Whose.
    :param kind: What it is.
    :param path: Where, relative to the bot's directory.
    :param title: What it is called.
    :param run_id: The iteration that produced it, for a report.
    :param created_at: Epoch seconds.
    """

    id: str
    bot_id: str
    kind: DocKind
    path: str
    title: str
    run_id: str | None = None
    created_at: int = 0


class Workspace:
    """A bot's directory, and the index over what is in it.

    :param bots: Where bots live.
    :param root: Parent directory for every bot's folder.
    """

    def __init__(self, bots: BotStore, root: Path | str = DEFAULT_ROOT) -> None:
        self.bots = bots
        self.store = bots.store
        self.root = Path(root).expanduser()
        with self.store.atomic() as conn:
            for statement in _statements(_SCHEMA):
                conn.execute(statement)

    def path_for(self, bot: Bot) -> Path:
        """
        Where this bot's directory is.

        The bot's own ``workspace`` wins when it has one, so an operator can
        point a bot at an existing checkout rather than a fresh folder.

        :param bot: The bot.
        :returns: Its directory.
        """
        return Path(bot.workspace).expanduser() if bot.workspace else self.root / bot.slug

    def prepare(self, bot: Bot, *, now: int, source_repo: Path | None = None) -> Path:
        """
        Create the bot's directory and seed its documentation.

        Idempotent: an existing directory is adopted rather than overwritten,
        and an existing file is never rewritten. A bot's charter is something a
        person edits, and a "setup" step that silently reverts those edits on
        restart is worse than no setup step.

        :param bot: The bot.
        :param now: Epoch seconds.
        :param source_repo: A repository to add a worktree from, so the bot
            works in its own branch of a shared codebase rather than an
            unrelated folder.
        :returns: The directory.
        """
        target = self.path_for(bot)
        if source_repo is not None and not target.exists():
            self._add_worktree(bot, source_repo, target)
        target.mkdir(parents=True, exist_ok=True)

        for name, template in _SEED.items():
            path = target / name
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                template.format(
                    display_name=bot.display_name,
                    mission=bot.mission,
                    persona=bot.persona,
                    wake=bot.wake.to_dict(),
                    slug=bot.slug,
                )
            )
        for name, kind in (
            ("charter.md", DocKind.CHARTER),
            ("runbook.md", DocKind.RUNBOOK),
            ("lessons.md", DocKind.LESSON),
        ):
            self.index(bot, kind, name, title=name.removesuffix(".md").title(), now=now)
        return target

    def _add_worktree(self, bot: Bot, source_repo: Path, target: Path) -> None:
        """
        Give the bot its own checkout on its own branch.

        Best-effort. A repository that cannot make a worktree can still be
        worked in directly, and failing the bot over it would be worse than
        saying so — but it is said, because the isolation the roster promises
        is now absent.

        :param bot: The bot.
        :param source_repo: The repository to branch from.
        :param target: Where the worktree goes.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "worktree", "add", "-b", f"bots/{bot.slug}", str(target)],
            cwd=str(source_repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            _logger.warning(
                "could not create a worktree for %s (%s); it will work in a plain directory "
                "and share whatever checkout it is pointed at",
                bot.slug,
                result.stderr.strip()[:160],
            )

    def index(
        self,
        bot: Bot,
        kind: DocKind,
        path: str,
        *,
        title: str,
        now: int,
        run_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Doc:
        """
        Record that a document exists, so the bot can find it later.

        :param bot: Whose.
        :param kind: What it is.
        :param path: Relative to the bot's directory.
        :param title: What it is called.
        :param now: Epoch seconds.
        :param run_id: The iteration that produced it.
        :param conn: Join an open transaction, or ``None``.
        :returns: The index entry.
        """
        doc = Doc(
            id=uuid.uuid4().hex,
            bot_id=bot.id,
            kind=kind,
            path=path,
            title=title,
            run_id=run_id,
            created_at=now,
        )
        with self.bots._tx(conn) as conn:
            conn.execute(
                "INSERT INTO bot_docs (id, bot_id, kind, path, title, run_id, created_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(bot_id, path) DO UPDATE SET"
                " title = excluded.title, run_id = COALESCE(excluded.run_id, run_id)",
                (
                    doc.id,
                    doc.bot_id,
                    doc.kind.value,
                    doc.path,
                    doc.title,
                    doc.run_id,
                    doc.created_at,
                ),
            )
        return doc

    def docs(self, bot: Bot, *, kind: DocKind | None = None) -> list[Doc]:
        """
        What this bot has written, newest first.

        :param bot: Whose.
        :param kind: Restrict to one sort, or ``None`` for all.
        :returns: The index entries.
        """
        sql = "SELECT * FROM bot_docs WHERE bot_id = ?"
        params: list[object] = [bot.id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind.value)
        with self.store.atomic() as conn:
            rows = conn.execute(sql + " ORDER BY created_at DESC", params).fetchall()
        return [_row_to_doc(row) for row in rows]

    def write_report(
        self,
        bot: Bot,
        title: str,
        body: str,
        *,
        run_id: str,
        now: int,
    ) -> Doc:
        """
        File a report, citing the iteration that produced it.

        The citation is not decoration. A report nobody can trace back to a run
        is an assertion; one that names its run can be checked against what
        actually happened, which is the whole difference between a record and a
        claim.

        :param bot: Whose report.
        :param title: What it is about.
        :param body: Markdown.
        :param run_id: The iteration that produced it.
        :param now: Epoch seconds.
        :returns: The index entry.
        """
        directory = self.path_for(bot) / "reports"
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{now}-{_slugify(title)}.md"
        (directory / name).write_text(
            f"# {title}\n\n> Produced by `{bot.slug}` in run `{run_id[:12]}`.\n\n{body.strip()}\n"
        )
        return self.index(
            bot,
            DocKind.REPORT,
            f"reports/{name}",
            title=title,
            now=now,
            run_id=run_id,
        )


def _slugify(title: str) -> str:
    """Turn a title into a filename that needs no escaping anywhere."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return cleaned[:60] or "report"


def _row_to_doc(row: sqlite3.Row) -> Doc:
    """Rebuild a :class:`Doc` from its row."""
    return Doc(
        id=row["id"],
        bot_id=row["bot_id"],
        kind=DocKind(row["kind"]),
        path=row["path"],
        title=row["title"],
        run_id=row["run_id"],
        created_at=row["created_at"],
    )
