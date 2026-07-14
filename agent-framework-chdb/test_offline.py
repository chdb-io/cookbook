"""Offline verification for the chDB + Agent Framework recipe.

Drives an Agent with a scripted fake chat client (no API key needed) to
prove the two claims the recipe makes:

1. A wrong query comes back to the model as the engine's real error
   (type + code + message inside the JSON envelope), and the agent run
   continues.
2. Control case: a tool that RAISES instead of returning gets swallowed by
   Agent Framework into the generic "Error: Function failed." — which is
   why the chDB tools never raise.

Run: python test_offline.py
"""

import asyncio
import inspect
import json
from collections.abc import Sequence
from typing import Any

from agent_framework import (
    Agent,
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
    tool,
)

from chdb_tools import chdb_maf_tools


class ScriptedChatClient(FunctionInvocationLayer, BaseChatClient):
    """Replays a scripted list of assistant turns; records what it saw."""

    def __init__(self, script: list[list[Content]]) -> None:
        super().__init__()
        self.script = script
        self.turn = 0
        self.seen_messages: list[Sequence[Message]] = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        async def respond() -> ChatResponse:
            self.seen_messages.append(list(messages))
            contents = self.script[self.turn]
            self.turn += 1
            return ChatResponse(messages=Message(role="assistant", contents=contents))

        return respond()


def function_results(client: ScriptedChatClient) -> list[str]:
    """Function-result payloads the model was shown, in call order.

    The last request to the client carries the full accumulated history,
    so reading only that batch avoids double-counting earlier turns.
    """
    return [
        str(content.result)
        for message in client.seen_messages[-1]
        for content in message.contents
        if content.type == "function_result"
    ]


async def main() -> None:
    tools = chdb_maf_tools()

    # --- 1. wrong SQL: engine error reaches the model, run recovers -------
    client = ScriptedChatClient(
        [
            [Content.from_function_call(call_id="c1", name="run_select_query",
                                        arguments={"sql": "SELEC bad"})],
            [Content.from_function_call(call_id="c2", name="run_select_query",
                                        arguments={"sql": "SELECT 21 * 2 AS answer"})],
            [Content.from_text("The answer is 42.")],
        ]
    )
    agent = Agent(client=client, name="analyst", tools=tools)
    run = agent.run("What is 21 * 2?")
    result = await run if inspect.isawaitable(run) else run

    payloads = function_results(client)
    error_envelope = json.loads(payloads[0])
    ok_envelope = json.loads(payloads[1])

    assert error_envelope["ok"] is False
    assert error_envelope["error"]["type"] == "UNKNOWN_IDENTIFIER"
    assert error_envelope["error"]["code"] == 47
    assert "Error: Function failed." not in payloads[0]
    assert ok_envelope["result"]["rows"] == [{"answer": 42}]
    assert "42" in result.text
    print("1. engine error reached the model as a typed envelope; run recovered: OK")

    # --- 2. control: a raising tool is swallowed into a generic message ---
    @tool(name="raising_tool", description="Control case: raises instead of returning.")
    async def raising_tool(sql: str) -> str:
        raise RuntimeError(f"engine exploded on {sql!r}")

    control_client = ScriptedChatClient(
        [
            [Content.from_function_call(call_id="c1", name="raising_tool",
                                        arguments={"sql": "SELECT 1"})],
            [Content.from_text("done")],
        ]
    )
    control_agent = Agent(client=control_client, name="control", tools=[raising_tool])
    run = control_agent.run("go")
    _ = await run if inspect.isawaitable(run) else run

    control_payloads = function_results(control_client)
    assert control_payloads, "expected a function result for the control tool"
    assert "Error: Function failed." in control_payloads[0]
    assert "engine exploded" not in control_payloads[0]
    print("2. raising tool was swallowed to 'Error: Function failed.' "
          "(why chDB tools return envelopes instead): OK")

    print("OFFLINE VERIFICATION OK")


if __name__ == "__main__":
    asyncio.run(main())
