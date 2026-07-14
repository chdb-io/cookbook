"""Offline verification for the chDB + DSPy recipe (no API key needed).

Drives dspy.ReAct with a scripted DummyLM to prove:

1. The typed callables from chdb_tools.py register as ReAct tools as-is —
   no adapter class, no schema writing.
2. A wrong query lands in the trajectory as chDB's typed error envelope
   (type + code + message), and the scripted model corrects it on the next
   step — the run recovers instead of dying.

Run: python test_offline.py
"""

import json

import dspy
from dspy.utils.dummies import DummyLM

from chdb_tools import chdb_dspy_tools


def main() -> None:
    tools = chdb_dspy_tools()
    agent = dspy.ReAct("question -> answer", tools=tools)

    assert set(agent.tools) == {
        "run_select_query",
        "list_databases",
        "list_tables",
        "describe_table",
        "get_sample_data",
        "list_functions",
        "finish",
    }
    print("1. typed callables registered as ReAct tools without adapters: OK")

    lm = DummyLM(
        [
            # step 1: the model writes broken SQL
            {"next_thought": "Query the answer.",
             "next_tool_name": "run_select_query",
             "next_tool_args": {"sql": "SELEC 21*2 AS x"}},
            # step 2: it reads the typed error from the observation and retries
            {"next_thought": "SYNTAX_ERROR — my SQL had a typo; fix it.",
             "next_tool_name": "run_select_query",
             "next_tool_args": {"sql": "SELECT 21*2 AS x"}},
            {"next_thought": "Got 42.", "next_tool_name": "finish", "next_tool_args": {}},
            {"reasoning": "The corrected query returned 42.", "answer": "42"},
        ]
    )
    dspy.configure(lm=lm)
    result = agent(question="What is 21*2?")

    first_observation = json.loads(result.trajectory["observation_0"])
    second_observation = json.loads(result.trajectory["observation_1"])

    assert first_observation["ok"] is False
    assert first_observation["error"]["type"] == "SYNTAX_ERROR"
    assert first_observation["error"]["code"] == 62
    assert second_observation["ok"] is True
    assert second_observation["result"]["rows"] == [{"x": 42}]
    assert result.answer == "42"
    print("2. typed error envelope in the trajectory; scripted model corrected "
          "and recovered: OK")

    print("OFFLINE VERIFICATION OK")


if __name__ == "__main__":
    main()
