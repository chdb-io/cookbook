"""A Microsoft Agent Framework analyst agent over chDB.

Requires an OpenAI API key (or swap in any Agent Framework chat client):

    pip install agent-framework-core agent-framework-openai chdb
    export OPENAI_API_KEY=sk-...
    python example_agent.py [path/to/data.parquet]

The agent gets the chDB tool suite over one shared in-process engine:
schema discovery first, then read-only ClickHouse SQL with parameter
binding. No database server, connection string, or credentials.
"""

import asyncio
import sys

from agent_framework import Agent
from agent_framework_openai import OpenAIChatClient

from chdb_tools import chdb_maf_tools

INSTRUCTIONS = (
    "You are a data analyst. Discover the schema first (list_tables, "
    "describe_table, get_sample_data) before writing queries. Bind values "
    "with {name:Type} placeholders passed via `params` — never concatenate "
    "values into SQL. Tool results are JSON envelopes: on {\"ok\": false}, "
    "read error.type and error.message and correct your query."
)


async def main() -> None:
    if len(sys.argv) > 1:
        tools = chdb_maf_tools(attachments={"data": sys.argv[1]})
        question = "What does the `data` table contain? Give me three interesting facts."
    else:
        tools = chdb_maf_tools()
        question = (
            "Using ClickHouse SQL over numbers(1000000), estimate the density "
            "of primes below one million."
        )

    agent = Agent(
        client=OpenAIChatClient(model="gpt-4o-mini"),
        name="chdb-analyst",
        instructions=INSTRUCTIONS,
        tools=tools,
    )
    result = await agent.run(question)
    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
