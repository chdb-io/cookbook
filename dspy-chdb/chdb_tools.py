"""chDB tools for DSPy agents.

DSPy has no third-party tool-package convention: ``dspy.ReAct`` takes plain
Python callables with type hints and docstrings, and builds tool schemas
from them. So this file is the whole integration — copy it into your
project and hand the functions to ``dspy.ReAct``.

Every function dispatches through ``chdb.agents.ChDBTool.call()`` and
returns its JSON envelope as the observation::

    {"ok": true,  "result": {"rows": [...], "truncated": false, ...}}
    {"ok": false, "error": {"code": 47, "type": "UNKNOWN_IDENTIFIER",
                            "message": "Unknown expression identifier ..."}}

so a wrong query comes back to the model as a typed, correctable error in
the ReAct trajectory instead of an exception traceback.
"""

import json
from typing import Any, Callable

from chdb.agents import ChDBTool


def chdb_dspy_tools(
    path: str = ":memory:",
    *,
    read_only: bool = True,
    max_rows: int = 1000,
    max_bytes: int = 1_000_000,
    max_execution_time: int | None = None,
    file_allowlist: list[str] | None = None,
    attachments: dict[str, Any] | None = None,
    engine: ChDBTool | None = None,
) -> list[Callable[..., str]]:
    """Build the chDB tool suite for ``dspy.ReAct`` over one shared engine.

    All tools run against the same chDB session, so a table registered by
    attach_file (or declared via ``attachments``) is visible to
    run_select_query and the introspection tools. attach_file is included
    only for writable suites (``read_only=False``).

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

    def run_select_query(sql: str, params: dict | None = None) -> str:
        """Run a read-only ClickHouse SQL query with chDB, an in-process
        ClickHouse engine (1000+ functions, window functions, arrays, JSON).
        Bind values via `params` as {name:Type} placeholders (e.g.
        WHERE id = {id:Int64}); never concatenate values into the SQL. Read
        external data inline with table functions: file('path'), s3('url',
        'format'), url(...), postgresql(...). Returns a JSON envelope with
        rows and a `truncated` flag, or a typed engine error to correct from.
        Use list_tables / describe_table first to learn the schema."""
        return json.dumps(engine.call("run_select_query", {"sql": sql, "params": params}))

    def list_databases() -> str:
        """List the databases available in the chDB session."""
        return json.dumps(engine.call("list_databases", {}))

    def list_tables(database: str | None = None) -> str:
        """List the tables in a database (the current database if omitted)."""
        return json.dumps(engine.call("list_tables", {"database": database}))

    def describe_table(target: str, database: str | None = None) -> str:
        """Describe the columns and types of a table or a table-function
        expression, e.g. "events" or "s3('s3://b/f.parquet','Parquet')"."""
        return json.dumps(engine.call("describe_table", {"target": target, "database": database}))

    def get_sample_data(target: str, database: str | None = None, limit: int | None = None) -> str:
        """Return a few sample rows from a table or table-function expression,
        to see real values before querying (default 5 rows)."""
        return json.dumps(
            engine.call("get_sample_data", {"target": target, "database": database, "limit": limit})
        )

    def list_functions(like: str | None = None, limit: int | None = None) -> str:
        """List available ClickHouse SQL functions, optionally filtered by an
        ILIKE pattern such as "%array%" (default limit 200)."""
        return json.dumps(engine.call("list_functions", {"like": like, "limit": limit}))

    def attach_file(name: str, path: str, format: str | None = None) -> str:
        """Register a local file as a queryable named table (a view over
        file()). Only works on writable sessions; read-only sessions declare
        files via the attachments option at construction instead."""
        return json.dumps(engine.call("attach_file", {"name": name, "path": path, "format": format}))

    tools: list[Callable[..., str]] = [
        run_select_query,
        list_databases,
        list_tables,
        describe_table,
        get_sample_data,
        list_functions,
    ]
    if not getattr(engine, "read_only", True):
        tools.append(attach_file)
    return tools
