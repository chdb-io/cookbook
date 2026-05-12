"""Variants of the input-path benchmark to probe whether operation type
or DataFrame width changes the chDB-vs-DuckDB picture.

Tests run at 10 M rows on the NYC TLC base (19 cols), plus a synthetic
wide variant (60 cols) at 10 M rows.

  V1  COUNT(*)                              simplest possible op
  V2  Filter + COUNT                        single-column scan
  V3  GROUP BY (same as scale benchmark)    re-measured for cross-check
  V4  GROUP BY on 60-col wide DataFrame     does width help chDB?
"""

import glob, os, statistics, time
import duckdb, chdb
from chdb import session as chdb_session
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
TAXI_GLOB = sorted(glob.glob(os.path.join(HERE, "yellow_tripdata_2024-*.parquet")))


def load_df(target_rows):
    frames, accum = [], 0
    for f in TAXI_GLOB:
        frames.append(pd.read_parquet(f))
        accum += len(frames[-1])
        if accum >= target_rows:
            break
    df = pd.concat(frames, ignore_index=True)
    return df.iloc[:target_rows].copy() if len(df) > target_rows else df


def widen(df, target_cols):
    """Add duplicated numeric columns until we hit target_cols."""
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    while df.shape[1] < target_cols:
        src = numeric_cols[df.shape[1] % len(numeric_cols)]
        df[f"extra_{df.shape[1]}"] = df[src] * 1.001  # avoid bytes-identical dedup
    return df


def measure(fn, iters=5, warmup=1):
    for _ in range(warmup):
        fn()
    times = [time.perf_counter() for _ in range(iters * 2)]
    times = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times) * 1000


def run_variant(label, sql_chdb, sql_duck, df):
    duck_con = duckdb.connect()
    sess = chdb_session.Session()

    def duck_q():
        duck_con.register("trips_df", df)
        return duck_con.execute(sql_duck).df()

    def chdb_q():
        return sess.query(sql_chdb, "DataFrame")

    d = measure(duck_q)
    c = measure(chdb_q)
    spd = d / c
    winner = "chDB" if spd > 1.0 else "DuckDB"
    print(f"  {label:<40s} DuckDB {d:>7.1f} ms   chDB {c:>7.1f} ms   {spd:.2f}× ({winner})")


def main():
    print(f"\nDuckDB {duckdb.__version__}  /  chDB {chdb.__version__}  on Apple M5 Max")

    print(f"\n--- 10 M rows × 19 cols (NYC TLC) ---")
    df = load_df(10_000_000)
    print(f"  df: {df.shape[0]:,} rows × {df.shape[1]} cols, "
          f"{df.memory_usage(deep=False).sum()/1024/1024:.0f} MB")
    run_variant("V1 COUNT(*)",
        "SELECT count(*) FROM Python(df)",
        "SELECT count(*) FROM trips_df", df)
    run_variant("V2 filter + COUNT",
        "SELECT count(*) FROM Python(df) WHERE fare_amount > 50",
        "SELECT count(*) FROM trips_df WHERE fare_amount > 50", df)
    run_variant("V3 GROUP BY (cross-check)",
        """SELECT payment_type, count(*) c, avg(fare_amount), avg(tip_amount)
           FROM Python(df) WHERE fare_amount > 0 GROUP BY payment_type ORDER BY c DESC""",
        """SELECT payment_type, count(*) c, avg(fare_amount), avg(tip_amount)
           FROM trips_df WHERE fare_amount > 0 GROUP BY payment_type ORDER BY c DESC""", df)

    print(f"\n--- 10 M rows × 60 cols (synthetically widened) ---")
    df_wide = widen(df.copy(), 60)
    print(f"  df: {df_wide.shape[0]:,} rows × {df_wide.shape[1]} cols, "
          f"{df_wide.memory_usage(deep=False).sum()/1024/1024:.0f} MB")
    run_variant("V4 GROUP BY (wide DF)",
        """SELECT payment_type, count(*) c, avg(fare_amount), avg(tip_amount)
           FROM Python(df_wide) WHERE fare_amount > 0 GROUP BY payment_type ORDER BY c DESC""",
        """SELECT payment_type, count(*) c, avg(fare_amount), avg(tip_amount)
           FROM trips_df WHERE fare_amount > 0 GROUP BY payment_type ORDER BY c DESC""",
        df_wide)
    run_variant("V5 COUNT(*) (wide DF)",
        "SELECT count(*) FROM Python(df_wide)",
        "SELECT count(*) FROM trips_df", df_wide)


if __name__ == "__main__":
    main()
