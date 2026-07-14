"""chDB tools for Microsoft Agent Framework agents.

Builds Agent Framework ``FunctionTool``s over ``chdb.agents.ChDBTool``, the
agent-tool surface the chdb package ships. Tool names, descriptions, and JSON
schemas come from the descriptors bundled with chdb (its single source of
truth across bindings), so nothing here is hand-maintained.

The one rule that matters in Agent Framework: **return errors as strings,
never raise.** Agent Framework swallows tool exceptions into a generic
"Error: Function failed." message unless the user opts into
``include_detailed_errors=True`` — so a raised chDB error would reach the
model with no information to correct the query. Dispatching through
``ChDBTool.acall()`` satisfies the rule by construction: it returns a JSON
envelope for both success and failure::

    {"ok": true,  "result": {"rows": [...], "truncated": false, ...}}
    {"ok": false, "error": {"code": 47, "type": "UNKNOWN_IDENTIFIER",
                            "message": "Unknown expression identifier ..."}}

so the model always sees the engine's actual error type, code, and message.
"""

import json
from typing import Any

from agent_framework import FunctionTool
from chdb.agents import ChDBTool, tool_specs


def chdb_maf_tools(
    path: str = ":memory:",
    *,
    read_only: bool = True,
    max_rows: int = 1000,
    max_bytes: int = 1_000_000,
    max_execution_time: int | None = None,
    file_allowlist: list[str] | None = None,
    attachments: dict[str, Any] | None = None,
    engine: ChDBTool | None = None,
) -> list[FunctionTool]:
    """Build the chDB tool suite for Agent Framework over one shared engine.

    All returned tools run against the same chDB session, so a table
    registered by attach_file (or declared via ``attachments``) is visible to
    run_select_query and the introspection tools. attach_file is included
    only for writable suites (``read_only=False``); on a read-only session,
    declare files via ``attachments`` instead.

    Pass an existing ``chdb.agents.ChDBTool`` as ``engine`` to control its
    lifecycle yourself (the other arguments are then ignored).
    """
    if engine is None:
        engine = ChDBTool(
            path,
            read_only=read_only,
            max_rows=max_rows,
            max_bytes=max_bytes,
            max_execution_time=max_execution_time,
            file_allowlist=file_allowlist,
            attachments=attachments,
        )

    def make_dispatch(tool_name: str):
        async def dispatch(**arguments: Any) -> str:
            # acall never raises for engine errors: failures come back inside
            # the envelope, which is exactly what Agent Framework needs.
            envelope = await engine.acall(tool_name, arguments)
            return json.dumps(envelope)

        return dispatch

    writable = not getattr(engine, "read_only", True)
    tools: list[FunctionTool] = []
    for spec in tool_specs(dialect="openai"):
        fn = spec["function"]
        if fn["name"] == "attach_file" and not writable:
            continue
        tools.append(
            FunctionTool(
                name=fn["name"],
                description=fn["description"],
                func=make_dispatch(fn["name"]),
                input_model=fn["parameters"],
            )
        )
    return tools
