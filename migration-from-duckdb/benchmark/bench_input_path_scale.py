"""Test DataFrame INPUT path (existing pandas DataFrame → SQL → DataFrame)
at multiple scales, to check whether chDB catches up with DuckDB as row
count grows. The blog claims chDB wins 7:3 against DuckDB+pandas at 10M
rows on c6a.4xlarge; our Q13/Q15 (3M rows on M5 Max) show DuckDB 1.6-2×.
This script tests 3M / 10M / 18M on the same hardware.
"""

import glob
import os
import statistics
import time

import duckdb
import chdb
from chdb import session as chdb_session
import pandas as pd
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
TAXI_GLOB = sorted(glob.glob(os.path.join(HERE, "yellow_tripdata_2024-*.parquet")))


def load_df(target_rows):
    """Load enough monthly Parquet files to reach at least target_rows, then truncate."""
    frames = []
    accum = 0
    for f in TAXI_GLOB:
        frames.append(pd.read_parquet(f))
        accum += len(frames[-1])
        if accum >= target_rows:
            break
    df = pd.concat(frames, ignore_index=True)
    if len(df) > target_rows:
        df = df.iloc[:target_rows].copy()
    return df


# The SQL that's run on both engines — a simple group-by on the DataFrame,
# representative of what an agent might do after loading data.
SQL = """
SELECT payment_type,
       count(*) AS trips,
       avg(fare_amount) AS avg_fare,
       avg(tip_amount)  AS avg_tip
FROM Python(df) WHERE fare_amount > 0
GROUP BY payment_type ORDER BY trips DESC
"""

DUCK_SQL = SQL.replace("Python(df)", "trips_df")


def measure(name, fn, iters=5, warmup=1):
    # warm-up so engine init doesn't pollute first iter
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(iters):
        t0 = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times) * 1000  # ms


def run_scale(target_rows):
    print(f"\n{'='*60}")
    print(f"Scale: {target_rows:>11,} rows × 19 cols")
    print(f"{'='*60}")

    df = load_df(target_rows)
    print(f"  loaded df: {df.shape[0]:,} rows × {df.shape[1]} cols, "
          f"{df.memory_usage(deep=False).sum() / 1024 / 1024:.0f} MB in memory")

    # DuckDB input path: register + execute + .df()
    duck_con = duckdb.connect()
    def duck_query():
        duck_con.register("trips_df", df)
        return duck_con.execute(DUCK_SQL).df()

    # chDB input path: Python(df) table function
    chdb_sess = chdb_session.Session()
    def chdb_query():
        return chdb_sess.query(SQL, "DataFrame")

    duck_ms = measure("DuckDB register+execute+.df()", duck_query)
    chdb_ms = measure("chDB   Python(df) + sess.query", chdb_query)

    speedup_chdb = duck_ms / chdb_ms
    winner = "chDB" if speedup_chdb > 1.0 else "DuckDB"
    delta = abs(duck_ms - chdb_ms)

    print(f"  DuckDB: {duck_ms:>8.1f} ms")
    print(f"  chDB  : {chdb_ms:>8.1f} ms")
    print(f"  chDB speedup vs DuckDB: {speedup_chdb:.2f}× ({winner} wins by {delta:.1f} ms)")

    return {"rows": target_rows, "duck_ms": duck_ms, "chdb_ms": chdb_ms, "chdb_speedup": speedup_chdb}


def main():
    print(f"\nDuckDB {duckdb.__version__}  /  chDB {chdb.__version__}")
    print(f"Hardware: Apple M5 Max, 36 GB RAM, macOS")
    results = []
    for n in (3_000_000, 10_000_000, 18_000_000):
        results.append(run_scale(n))

    print(f"\n\n{'SUMMARY':-^60}")
    print(f"{'Rows':>12}  {'DuckDB':>10}  {'chDB':>10}  {'chDB speedup':>14}")
    print("-" * 60)
    for r in results:
        print(f"{r['rows']:>12,}  {r['duck_ms']:>8.1f} ms  {r['chdb_ms']:>8.1f} ms  {r['chdb_speedup']:>13.2f}×")


if __name__ == "__main__":
    main()
