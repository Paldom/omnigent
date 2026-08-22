"""What an MCP server receives after a person picks an option.

Drives omnigent's real inline-elicitation callback — the same function the
runner uses when an MCP server sends elicitation/create mid tools/call —
and prints the ElicitResult that would go back over the wire.
"""

import asyncio
import inspect
from typing import Any

from mcp.types import ElicitRequestFormParams

from omnigent.runner import pending_approvals
from omnigent.runner.mcp_manager import RunnerMcpManager

EID = "elicit_demo"
SESSION = "conv_demo"
SCHEMA: Any = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["dev", "staging", "prod"]}},
}
PICKED = "prod"


class _Server:
    async def post(self, url: str, json: Any = None, timeout: float = 30.0) -> Any:
        class R:
            @staticmethod
            def raise_for_status() -> None: ...
            @staticmethod
            def json() -> dict[str, str]:
                return {"elicitation_id": EID}
        return R()


def _deliver_like_the_runner() -> None:
    """Mirror omnigent/runner/app.py's `approval` event branch."""
    data = {"elicitation_id": EID, "action": "accept", "content": {"answer": PICKED}}
    accepted = data["action"] == "accept"
    if len(inspect.signature(pending_approvals.resolve).parameters) >= 3:
        pending_approvals.resolve(data["elicitation_id"], accepted, data.get("content"))
    else:
        # The old registry is Future[bool]; there is nowhere to put the answer.
        pending_approvals.resolve(data["elicitation_id"], accepted)


async def main() -> None:
    mgr = RunnerMcpManager(server_client=_Server())  # type: ignore[arg-type]
    callback = mgr._build_elicitation_callback()
    params = ElicitRequestFormParams(
        message="Which environment should I deploy to?", requestedSchema=SCHEMA
    )
    task = asyncio.ensure_future(callback(SESSION, params))
    for _ in range(500):
        if EID in pending_approvals._pending:
            break
        await asyncio.sleep(0.001)
    _deliver_like_the_runner()
    result = await task
    got = (result.content or {}).get("answer")
    print(f"  person picked : {PICKED}")
    print(f"  server receives: {got}")
    print(f"  -> {'MATCH' if got == PICKED else 'WRONG — the schema answered for them'}")


asyncio.run(main())
