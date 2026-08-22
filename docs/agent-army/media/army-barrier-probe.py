"""What a barrier gate accepts, before and after.

Drives the real supervisor against a real SQLite store: a gate that declares
multi_select, answered in chat exactly as a person would type it.
"""

import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from army.state import Run, RunState  # noqa: E402
from army.store import Store  # noqa: E402
from army.supervisor import Supervisor  # noqa: E402
from tests.army.test_supervisor import ChattyOmni, DemoWorkload, _drive_to_waiting  # noqa: E402


class Snacks(DemoWorkload):
    name = "snacks"
    multi_select = True

    def evaluate(self, run: Run) -> tuple[str, list[str], dict[str, Any]]:
        return "Pick snacks", ["popcorn", "pretzels", "olives"], dict(run.artifacts)

    def apply(self, run: Run, decision: str, payload: dict[str, Any]) -> tuple[str, str]:
        if decision == "deny":
            return "paused", "declined"
        return "continue", f"chose {', '.join(payload.get('choices') or [])}"


def answer(reply: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "army.db")
        omni, workload = ChattyOmni(), Snacks()
        run = _drive_to_waiting(store, omni, workload)
        omni.replies = [reply]
        Supervisor(store, omni, workload).tick()
        after = store.get_run(run.id)
        assert after is not None
        if after.state is RunState.WAITING_HUMAN:
            return "still parked — not read as an answer"
        return f"{after.state.value}: {after.terminal_reason}"


for reply in sys.argv[1:]:
    print(f'  "{reply}"')
    print(f"      -> {answer(reply)}")
