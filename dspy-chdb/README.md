# chDB tools for DSPy

Give [DSPy](https://github.com/stanfordnlp/dspy) ReAct agents analytical SQL over local files (Parquet/CSV/JSON), object storage, and remote databases with [chDB](https://clickhouse.com/docs/en/chdb), the in-process ClickHouse engine. No server to start, no connection string, no credentials.

## Why this is a cookbook, not a package

DSPy has no third-party tool-package convention: `dspy.ReAct` accepts plain Python callables and derives tool schemas from their type hints and docstrings. The whole integration is one copyable file — **`chdb_tools.py`** — which defines typed wrapper functions over `chdb.agents.ChDBTool`.

## Files

- **`chdb_tools.py`** — `chdb_dspy_tools()` returns the chDB tool suite as typed callables sharing one engine.
- **`example_agent.py`** — a ReAct analyst over your Parquet/CSV file (needs an LM API key).
- **`test_offline.py`** — verification with a scripted `DummyLM`, no API key needed.

## Quick start

```shell
pip install dspy chdb
```

```python
import dspy
from chdb_tools import chdb_dspy_tools

dspy.configure(lm=dspy.LM("openai/gpt-4o-mini"))

agent = dspy.ReAct(
    "question -> answer",
    tools=chdb_dspy_tools(attachments={"events": "data/events.parquet"}),
)
print(agent(question="What are the top 5 event types by count?").answer)
```

The suite exposes `run_select_query` (read-only ClickHouse SQL with `{name:Type}` parameter binding), `list_databases`, `list_tables`, `describe_table`, `get_sample_data`, `list_functions`, and — with `read_only=False` — `attach_file`. All tools share one engine, so what one tool attaches the others see.

## Error handling in the trajectory

Every tool returns the JSON envelope from `ChDBTool.call()` as its observation. A wrong query doesn't raise — the model reads the engine's typed error in the next ReAct step and corrects itself:

```json
{"ok": false, "error": {"code": 62, "type": "SYNTAX_ERROR",
                        "message": "Syntax error: failed at position 7 ..."}}
```

`test_offline.py` proves the loop end to end with a scripted `DummyLM`: broken SQL → typed error in `trajectory["observation_0"]` → corrected SQL → answer.

```shell
python test_offline.py
```

## Safety defaults

- **Read-only by default.** Sessions are locked with the ClickHouse `readonly=2` setting: `INSERT`/`CREATE`/`ALTER`/`DROP` are rejected by the engine (not by prompt), while `SELECT` and the `file()`/`s3()`/`url()` table functions keep working.
- **Capped results.** `max_rows` (default 1000) and `max_bytes` (default 1 MB) bound every observation; truncated results carry a `truncated` flag. `max_execution_time` adds an engine-side wall-clock limit.
- **Optional source allowlist.** With `file_allowlist=["/data/"]`, file-like sources may only read under the listed prefixes and DSN-based sources are refused.

## Sharing and lifecycle

```python
from chdb.agents import ChDBTool

engine = ChDBTool("analytics_dir", read_only=False)
tools = chdb_dspy_tools(engine=engine)
...
engine.close()
```
