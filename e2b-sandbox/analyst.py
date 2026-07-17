"""chDB on E2B walkthrough: create → seed 1M rows → query → pause → resume → verify.

Prints per-step timings, kills the sandbox at the end.

    pip install e2b-code-interpreter
    export E2B_API_KEY=e2b_...
    python analyst.py            # prebuilt "chdb" template (build: see README)
    python analyst.py --pip      # base sandbox + runtime `pip install chdb` instead
    CHDB_TEMPLATE=x analyst.py   # custom template name
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from e2b_code_interpreter import Sandbox

DEMO_METADATA = {"demo": "chdb-e2b-cookbook"}  # teardown.sh kills sandboxes by this tag

SEED = """
from chdb.session import Session
sess = Session("/home/user/chdb-data")
sess.query("CREATE DATABASE IF NOT EXISTS demo")
sess.query('''CREATE TABLE IF NOT EXISTS demo.events
  (id UInt64, event_type LowCardinality(String), ts DateTime)
  ENGINE = MergeTree ORDER BY id''')
sess.query('''INSERT INTO demo.events
  SELECT number, ['view','click','purchase','signup','refund'][number % 5 + 1], now()
  FROM numbers(1000000)''')
print("rows:", str(sess.query("SELECT count() FROM demo.events", "CSV")).strip())
sess.close()
"""

AGGREGATE = """
from chdb.session import Session
sess = Session("/home/user/chdb-data")
print(sess.query('''SELECT event_type, count() AS n, round(100.0 * count() / sum(count()) OVER (), 1) AS pct
  FROM demo.events GROUP BY event_type ORDER BY n DESC''', "Pretty"))
sess.close()
"""

RECHECK = """
from chdb.session import Session
sess = Session("/home/user/chdb-data")
print("rows after resume:", str(sess.query("SELECT count() FROM demo.events", "CSV")).strip())
sess.query("INSERT INTO demo.events SELECT number + 1000000, 'view', now() FROM numbers(1000)")
print("rows after append:", str(sess.query("SELECT count() FROM demo.events", "CSV")).strip())
sess.close()
"""


def stdout_of(execution) -> str:
    out = execution.logs.stdout
    return "".join(out) if isinstance(out, list) else str(out)


def timed(label: str, fn):
    t0 = time.time()
    result = fn()
    print(f"[{time.time() - t0:6.2f}s] {label}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pip",
        action="store_true",
        help="use the base code-interpreter template and pip-install chdb at runtime",
    )
    args = parser.parse_args()

    if not os.environ.get("E2B_API_KEY"):
        print("E2B_API_KEY is not set — get one at https://e2b.dev/dashboard", file=sys.stderr)
        return 1

    template = None if args.pip else os.environ.get("CHDB_TEMPLATE", "chdb")

    sbx = timed(
        f"create sandbox (template={template or 'base'})",
        lambda: Sandbox.create(template=template, metadata=DEMO_METADATA),
    )
    print(f"          sandbox id: {sbx.sandbox_id}")

    try:
        if args.pip:
            timed(
                "pip install chdb (runtime install, ~8s)",
                lambda: sbx.commands.run("pip install --no-cache-dir chdb", timeout=600),
            )

        # Pays the Jupyter-kernel cold start (~5s steady state; slower once per
        # template on a fresh node — see README).
        ex = timed(
            "first query (kernel start + import chdb + SELECT 1)",
            lambda: sbx.run_code("import chdb; print(chdb.query('SELECT 1', 'CSV'))"),
        )

        ex = timed("seed 1M-row MergeTree table on sandbox disk", lambda: sbx.run_code(SEED))
        print("          " + stdout_of(ex).strip())

        ex = timed("aggregate query", lambda: sbx.run_code(AGGREGATE))
        print(stdout_of(ex))

        timed("pause(keep_memory=True)", lambda: sbx.pause(keep_memory=True))
        timed("resume (connect)", lambda: sbx.connect())

        ex = timed("verify data survived + append 1000 rows", lambda: sbx.run_code(RECHECK))
        for line in stdout_of(ex).strip().splitlines():
            print("          " + line)

        ex = timed("query again after resume", lambda: sbx.run_code(AGGREGATE))
        print(stdout_of(ex))
    finally:
        try:
            sbx.kill()
            print("sandbox killed.")
        except Exception as exc:  # best-effort; ./teardown.sh catches strays
            print(f"cleanup warning: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
