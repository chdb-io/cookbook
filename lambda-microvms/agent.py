"""A data analyst agent in 50 lines: Claude + one tool + chDB.

The whole "database" is an import — chDB runs ClickHouse inside this process,
so the agent's only tool is execute_sql and there is nothing else to deploy.
Run it directly for a terminal analyst over the baked store:

    CHDB_DATA_PATH=./chdb-data ANTHROPIC_API_KEY=sk-... python agent.py

main.py mounts the same ask() behind /ask when this runs on Lambda MicroVMs.
"""
import os
import sys

import anthropic

MODEL = os.getenv("AGENT_MODEL", "claude-opus-4-8")
MAX_TOOL_ROUNDS = 12  # cap tool-use rounds so a runaway question can't burn budget

SYSTEM = """You are a data analyst with an embedded ClickHouse engine (chDB) in your process.
demo.hits holds web analytics events (the ClickBench dataset): one row per page hit, with
EventDate, CounterID (site id), UserID, URL, Referer, SearchPhrase, RegionID, OS, IsMobile,
ResolutionWidth and ~100 more columns. Answer questions by writing ClickHouse SQL: prefer
aggregates, LIMIT any raw-row output. You can also reach live external data in the same SQL,
e.g. s3('https://clickhouse-public-datasets.s3.amazonaws.com/hits_compatible/athena_partitioned/hits_1.parquet', NOSIGN).
To remember expensive results for later questions, materialize them:
CREATE TABLE demo.<name> ENGINE = MergeTree ORDER BY tuple() AS SELECT ..."""

TOOLS = [{
    "name": "execute_sql",
    "description": "Run one ClickHouse SQL statement on the in-process engine; returns JSON.",
    "input_schema": {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "A single SQL statement"}},
        "required": ["sql"],
    },
}]

client = anthropic.Anthropic()


def ask(question: str, history: list, execute_sql) -> str:
    """One analyst turn: history is mutated in place, execute_sql runs SQL -> str."""
    history.append({"role": "user", "content": question})
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=MODEL, max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=history)
        history.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")
        results = []
        for block in response.content:
            if block.type == "tool_use":
                try:
                    out = execute_sql(block.input["sql"])
                    if len(out) > 8000:  # keep tool payloads bounded; mark when clipped
                        out = out[:8000] + "\n\u2026[truncated]"
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": out})
                except Exception as exc:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(exc)[:2000], "is_error": True})
        history.append({"role": "user", "content": results})
    return "Stopped after the maximum number of tool-use rounds without a final answer."


if __name__ == "__main__":
    from chdb import session as chdb_session
    sess = chdb_session.Session(os.getenv("CHDB_DATA_PATH", "/app/chdb-data"))
    history = []
    print("chDB analyst ready — ask about demo.hits (Ctrl-D to exit)")
    for line in sys.stdin:
        if line.strip():
            print(ask(line.strip(), history, lambda sql: sess.query(sql, "JSONCompact").data()))
