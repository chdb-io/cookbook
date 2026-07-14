# chDB tools for Microsoft Agent Framework

Give [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) agents analytical SQL over local files (Parquet/CSV/JSON), object storage, and remote databases with [chDB](https://clickhouse.com/docs/en/chdb), the in-process ClickHouse engine. The engine runs inside the Python process — no server to start, no connection string, no credentials to configure.

## Files

- **`chdb_tools.py`** — the reusable adapter: `chdb_maf_tools()` builds Agent Framework `FunctionTool`s over `chdb.agents.ChDBTool`. Tool names, descriptions, and JSON schemas come straight from the descriptors bundled with the `chdb` package, so nothing is hand-maintained.
- **`example_agent.py`** — an analyst agent over your Parquet/CSV file (needs `OPENAI_API_KEY`).
- **`test_offline.py`** — verification that runs the full agent loop against a scripted fake chat client, no API key needed.

## Quick start

```shell
pip install agent-framework-core agent-framework-openai chdb
```

```python
from agent_framework import Agent
from agent_framework_openai import OpenAIChatClient

from chdb_tools import chdb_maf_tools

agent = Agent(
    client=OpenAIChatClient(model="gpt-4o-mini"),
    name="analyst",
    tools=chdb_maf_tools(attachments={"events": "data/events.parquet"}),
)
result = await agent.run("What are the top 5 event types by count?")
```

The suite exposes `run_select_query` (read-only ClickHouse SQL with `{name:Type}` parameter binding), `list_databases`, `list_tables`, `describe_table`, `get_sample_data`, `list_functions`, and — on writable sessions (`read_only=False`) — `attach_file`. All tools share one engine, so what one tool attaches the others see.

## The one rule that matters: return errors, don't raise

Agent Framework swallows tool exceptions. A tool that raises produces this, verbatim, as the model-visible result (from `agent_framework/_tools.py`, verified against 1.11.0):

```
Error: Function failed.
```

The model learns nothing about *why* — it can't correct the query, and unless the developer opts into `include_detailed_errors=True` (a debugging flag, off by default), the detail never reaches it.

That's why the adapter dispatches every call through `ChDBTool.acall()`, which **returns** a JSON envelope for both success and failure instead of raising:

```json
{"ok": false, "error": {"code": 47, "type": "UNKNOWN_IDENTIFIER",
                        "message": "Unknown expression identifier `SELEC` ..."}}
```

The model reads the engine's real error type, code, and message, and fixes its own SQL on the next turn. `test_offline.py` proves both halves: the envelope path recovers, and a control tool that raises gets flattened to `Error: Function failed.`

```shell
python test_offline.py
# 1. engine error reached the model as a typed envelope; run recovered: OK
# 2. raising tool was swallowed to 'Error: Function failed.' (why chDB tools return envelopes instead): OK
```

## Safety defaults

- **Read-only by default.** Sessions are locked with the ClickHouse `readonly=2` setting: `INSERT`/`CREATE`/`ALTER`/`DROP` are rejected by the engine (not by prompt), while `SELECT` and the `file()`/`s3()`/`url()` table functions keep working.
- **Capped results.** `max_rows` (default 1000) and `max_bytes` (default 1 MB) bound every payload; truncated results carry a `truncated` flag the agent can react to. `max_execution_time` adds an engine-side wall-clock limit.
- **Optional source allowlist.** With `file_allowlist=["/data/"]`, file-like sources may only read under the listed prefixes and DSN-based sources (`postgresql()`, `mysql()`, `remote()`, …) are refused.

## Sharing and lifecycle

To control the engine's lifecycle yourself — or to share one session with other code in the same process — create it explicitly and inject it:

```python
from chdb.agents import ChDBTool

engine = ChDBTool("analytics_dir", read_only=False)
tools = chdb_maf_tools(engine=engine)
...
engine.close()
```

## Alternative: MCP

Agent Framework treats MCP as a first-class tool source (`MCPStdioTool`, `MCPStreamableHTTPTool`). If you prefer an out-of-process setup or already run [mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse), you can point `MCPStdioTool` at it instead of using the in-process adapter — at the cost of a separate server process and per-process state. The in-process adapter above is the recommended path for local analytics.

## Legacy note: AutoGen

AutoGen is in maintenance mode; Agent Framework is its successor. If you're still on AutoGen, chDB works through `LangChainToolAdapter` with the `langchain-chdb` package.
