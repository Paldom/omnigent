"""Agent Army — a durable control plane for an orchestrator built on Omnigent.

Omnigent owns sessions, harnesses, policies, sandboxes and the UI. It does not
own the *executable continuation* of a long-running loop: a durable schedule, a
durable transcript and a durable approval row do not jointly make a durable
workflow, because none of them records where the loop had got to.

This package owns exactly that one thing. A :class:`~army.state.Run` is one
iteration, its state advances by compare-and-swap on a monotonic version, and a
human verdict arrives as a durable :class:`~army.state.Command` rather than as
the resolution of a coroutine that a restart has already destroyed. On restart
the supervisor reads the same rows and keeps going; there is no separate
recovery path to get wrong.

Exactly one component owns each kind of state: Omnigent owns session state,
this package owns workflow state, and the boundary between them is Omnigent's
HTTP API (:mod:`army.omni`).
"""

from army.state import Command, Run, RunState

__all__ = ["Command", "Run", "RunState"]
