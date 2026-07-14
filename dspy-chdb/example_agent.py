"""A DSPy ReAct analyst agent over chDB.

Requires an OpenAI API key (or configure any DSPy-supported LM):

    pip install dspy chdb
    export OPENAI_API_KEY=sk-...
    python example_agent.py [path/to/data.parquet]
"""

import sys

import dspy

from chdb_tools import chdb_dspy_tools


def main() -> None:
    dspy.configure(lm=dspy.LM("openai/gpt-4o-mini"))

    if len(sys.argv) > 1:
        tools = chdb_dspy_tools(attachments={"data": sys.argv[1]})
        question = "What does the `data` table contain? Give me three interesting facts."
    else:
        tools = chdb_dspy_tools()
        question = (
            "Using ClickHouse SQL over numbers(1000000), estimate the density "
            "of primes below one million."
        )

    agent = dspy.ReAct("question -> answer", tools=tools)
    result = agent(question=question)
    print(result.answer)


if __name__ == "__main__":
    main()
