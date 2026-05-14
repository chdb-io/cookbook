"""DuckDB workload — aligned with §2 ordering of migration-from-duckdb.md.

Mirror of workload_aligned_chdb.py, using DuckDB-native idioms.

  Q1-Q3  JSON path access (`payload::JSON`, auto-extract paths)
  Q4     pandas baseline (DataStore comparison: chDB-side uses DataStore, DuckDB-side uses raw pandas)
  Q5     vector cosine via `array_cosine_distance(...)::FLOAT[384]`
  Q6     funnel via LAG CTE
  Q7     sequence pattern via LAG CTE
  Q8     approx_quantile list-form (single sketch)
  Q9-Q13 baseline analytical SQL
  Q14    GROUP BY + ORDER + LIMIT (exact, vs chDB's approximate topK)
  Q15    wide DataFrame round-trip
  Q16    CREATE TABLE AS + 5 queries
  Q17    range scan on indexed table
  Q18    single-file Parquet → DataFrame export
"""

import json, os, time
import duckdb
import pandas as pd
import psutil

HERE = os.path.dirname(os.path.abspath(__file__))
JSON_PARQUET   = os.path.join(HERE, "agent_events.parquet")
TAXI_SINGLE    = os.path.join(HERE, "yellow_tripdata_2024-01.parquet")
TAXI_GLOB      = os.path.join(HERE, "yellow_tripdata_2024-*.parquet")
VEC_PARQUET    = os.path.join(HERE, "embeddings.parquet")
QUERY_VEC      = json.load(open(os.path.join(HERE, "query_vec.json")))


def measure(name, fn):
    proc = psutil.Process(os.getpid())
    t0 = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - t0
    rss_mb = proc.memory_info().rss / 1024 / 1024
    rows = len(result) if hasattr(result, "__len__") else 1
    print(f"  {name:<46s} {elapsed*1000:>9.1f} ms   rows={rows:<6}   RSS={rss_mb:>7.1f} MB", flush=True)
    return {"name": name, "elapsed_ms": elapsed * 1000, "rows": rows, "rss_mb": rss_mb}


def main():
    print(f"\n=== DuckDB {duckdb.__version__} aligned workload ===", flush=True)
    con = duckdb.connect()
    results = []

    # ---------- §2.1 JSON ----------
    con.execute(f"""
        CREATE TABLE events AS
        SELECT id, payload::JSON AS data FROM read_parquet('{JSON_PARQUET}')
    """)
    results.append(measure("Q1 JSON GROUP BY response.status", lambda: con.execute("""
        SELECT data.response.status AS status, count(*) AS n
        FROM events GROUP BY status ORDER BY n DESC
    """).fetchall()))
    results.append(measure("Q2 JSON GROUP BY user.tier + avg(latency)", lambda: con.execute("""
        SELECT data.user.tier AS tier,
               avg(CAST(data.response.latency_ms AS DOUBLE)) AS avg_lat,
               count(*) AS n
        FROM events GROUP BY tier
    """).fetchall()))
    results.append(measure("Q3 JSON filter + group", lambda: con.execute("""
        SELECT data.tool AS tool, count(*) AS n
        FROM events
        WHERE data.user.region = '"us-east-1"'
        GROUP BY tool ORDER BY n DESC
    """).fetchall()))

    # ---------- §2.2 DataStore baseline (raw pandas — DuckDB has no DataStore equivalent) ----------
    df_pandas = pd.read_parquet(TAXI_SINGLE, columns=[
        "passenger_count", "fare_amount", "tip_amount", "trip_distance"
    ])
    results.append(measure("Q4 pandas groupby agg (DataStore baseline)", lambda:
        df_pandas.groupby("passenger_count").agg(
            avg_fare=("fare_amount", "mean"),
            sum_tip=("tip_amount", "sum"),
            avg_dist=("trip_distance", "mean"),
            n=("fare_amount", "count"),
        )
    ))

    # ---------- §2.3 AI-agent retrieval (vector) ----------
    con.execute(f"""
        CREATE TABLE vecs AS
        SELECT id, emb::FLOAT[384] AS emb FROM read_parquet('{VEC_PARQUET}')
    """)
    qv = "[" + ",".join(f"{x:.6f}" for x in QUERY_VEC) + "]"
    results.append(measure("Q5 vector top-10 cosine", lambda: con.execute(f"""
        SELECT id, array_cosine_distance(emb, {qv}::FLOAT[384]) AS d
        FROM vecs ORDER BY d ASC LIMIT 10
    """).fetchall()))

    # ---------- §2.6 Funnel / pattern (CTE simulation) ----------
    results.append(measure("Q6 funnel (CTE approx)", lambda: con.execute(f"""
        WITH events AS (
            SELECT PULocationID AS zone, tpep_pickup_datetime AS ts,
                   CASE WHEN fare_amount < 15 THEN 1
                        WHEN fare_amount > 50 THEN 2
                        WHEN Airport_fee > 0  THEN 3 END AS step
            FROM read_parquet('{TAXI_GLOB}')
            WHERE fare_amount < 15 OR fare_amount > 50 OR Airport_fee > 0
        ),
        with_lags AS (
            SELECT zone, ts, step,
                   LAG(step, 1) OVER (PARTITION BY zone ORDER BY ts) AS prev1,
                   LAG(ts,   1) OVER (PARTITION BY zone ORDER BY ts) AS prev1_ts,
                   LAG(step, 2) OVER (PARTITION BY zone ORDER BY ts) AS prev2,
                   LAG(ts,   2) OVER (PARTITION BY zone ORDER BY ts) AS prev2_ts
            FROM events
        ),
        levels AS (
            SELECT zone,
                   CASE WHEN step = 3 AND prev1 = 2 AND prev2 = 1
                             AND EXTRACT(EPOCH FROM (ts - prev2_ts)) < 3600 THEN 3
                        WHEN step = 2 AND prev1 = 1
                             AND EXTRACT(EPOCH FROM (ts - prev1_ts)) < 3600 THEN 2
                        WHEN step = 1 THEN 1
                        ELSE 0 END AS funnel_level
            FROM with_lags
        )
        SELECT funnel_level, count(DISTINCT zone) AS zones
        FROM levels WHERE funnel_level > 0
        GROUP BY funnel_level ORDER BY funnel_level
    """).fetchall()))
    results.append(measure("Q7 sequence pattern (CTE)", lambda: con.execute(f"""
        WITH events AS (
            SELECT PULocationID AS zone, tpep_pickup_datetime AS ts,
                   CASE WHEN fare_amount < 15 THEN 1
                        WHEN fare_amount > 50 THEN 2
                        WHEN fare_amount > 70 THEN 3 END AS step
            FROM read_parquet('{TAXI_GLOB}')
            WHERE fare_amount < 15 OR fare_amount > 50 OR fare_amount > 70
        ),
        with_lags AS (
            SELECT zone, step,
                   LAG(step, 1) OVER (PARTITION BY zone ORDER BY ts) AS prev1,
                   LAG(step, 2) OVER (PARTITION BY zone ORDER BY ts) AS prev2
            FROM events
        )
        SELECT count(*) FROM with_lags
        WHERE step = 3 AND prev1 = 2 AND prev2 = 1
    """).fetchall()))

    # ---------- §2.9 Many percentiles ----------
    # DuckDB supports a list form `approx_quantile(x, [0.5, 0.95, 0.99])` that
    # produces a single TDigest sketch — apples-to-apples with chDB's
    # quantilesTDigest(0.5, 0.95, 0.99)(x).
    results.append(measure("Q8 approx_quantile list-form p50/p95/p99", lambda: con.execute(f"""
        SELECT approx_quantile(fare_amount, [0.5, 0.95, 0.99]) AS pcts
        FROM read_parquet('{TAXI_GLOB}') WHERE fare_amount > 0
    """).fetchall()))

    # ---------- Baseline analytical SQL on Parquet ----------
    results.append(measure("Q9 aggregate", lambda: con.execute(f"""
        SELECT count(*) AS trips, sum(total_amount) AS revenue,
               avg(trip_distance) AS avg_distance
        FROM read_parquet('{TAXI_GLOB}')
    """).fetchall()))
    results.append(measure("Q10 group+filter", lambda: con.execute(f"""
        SELECT payment_type, count(*) AS trips, avg(tip_amount) AS avg_tip
        FROM read_parquet('{TAXI_GLOB}')
        WHERE fare_amount > 50 GROUP BY payment_type ORDER BY trips DESC
    """).fetchall()))
    results.append(measure("Q11 time bucket date_trunc", lambda: con.execute(f"""
        SELECT date_trunc('hour', tpep_pickup_datetime) AS hour, count(*) AS trips
        FROM read_parquet('{TAXI_GLOB}')
        GROUP BY hour ORDER BY hour LIMIT 24
    """).fetchall()))
    results.append(measure("Q12 approx_count_distinct", lambda: con.execute(f"""
        SELECT approx_count_distinct(PULocationID) AS unique_pickups,
               approx_count_distinct(DOLocationID) AS unique_dropoffs
        FROM read_parquet('{TAXI_GLOB}')
    """).fetchall()))

    df_narrow = pd.read_parquet(TAXI_SINGLE, columns=[
        "passenger_count", "trip_distance", "fare_amount", "tip_amount"
    ])
    def q13():
        con.register("trips_narrow", df_narrow)
        return con.execute("""
            SELECT passenger_count, count(*) AS n,
                   avg(fare_amount) AS avg_fare, avg(tip_amount) AS avg_tip
            FROM trips_narrow WHERE trip_distance BETWEEN 1 AND 20
            GROUP BY passenger_count ORDER BY n DESC
        """).df()
    results.append(measure("Q13 DataFrame roundtrip (narrow)", q13))

    # ---------- Reference queries ----------
    results.append(measure("Q14 topK pickup zones", lambda: con.execute(f"""
        SELECT PULocationID, count(*) AS c
        FROM read_parquet('{TAXI_GLOB}')
        GROUP BY PULocationID ORDER BY c DESC LIMIT 10
    """).fetchall()))

    df_wide = pd.read_parquet(TAXI_SINGLE)
    def q15():
        con.register("trips_wide", df_wide)
        return con.execute("""
            SELECT VendorID, count(*) AS trips,
                   avg(trip_distance) AS avg_dist, sum(fare_amount) AS total_fare,
                   sum(tip_amount) AS total_tip, avg(passenger_count) AS avg_pax
            FROM trips_wide WHERE fare_amount > 0
            GROUP BY VendorID ORDER BY trips DESC
        """).df()
    results.append(measure("Q15 DataFrame roundtrip (wide, 19 cols)", q15))

    def q16():
        con.execute("DROP TABLE IF EXISTS trips_perf_test")
        con.execute(f"""
            CREATE TABLE trips_perf_test AS
            SELECT VendorID, tpep_pickup_datetime, PULocationID, DOLocationID,
                   passenger_count, trip_distance, fare_amount, tip_amount, total_amount
            FROM read_parquet('{TAXI_GLOB}')
        """)
        return [
            con.execute("SELECT count(*) FROM trips_perf_test").fetchall(),
            con.execute("SELECT PULocationID, count(*) c FROM trips_perf_test GROUP BY PULocationID ORDER BY c DESC LIMIT 5").fetchall(),
            con.execute("SELECT VendorID, avg(fare_amount) FROM trips_perf_test GROUP BY VendorID").fetchall(),
            con.execute("SELECT date_trunc('day', tpep_pickup_datetime) d, count(*) FROM trips_perf_test GROUP BY d ORDER BY d LIMIT 10").fetchall(),
            con.execute("SELECT count(*) FROM trips_perf_test WHERE total_amount > 100").fetchall(),
        ]
    results.append(measure("Q16 persistent storage (load+5q)", q16))

    results.append(measure("Q17 PK range scan", lambda: con.execute("""
        SELECT count(*), avg(fare_amount), avg(tip_amount)
        FROM trips_perf_test
        WHERE tpep_pickup_datetime BETWEEN '2024-03-15 16:00:00' AND '2024-03-15 20:00:00'
    """).fetchall()))

    results.append(measure("Q18 Parquet → DataFrame export", lambda:
        con.execute(f"SELECT * FROM read_parquet('{TAXI_SINGLE}')").df()
    ))

    print("\n__RESULTS_JSON__", json.dumps({
        "engine": "duckdb", "version": duckdb.__version__, "queries": results,
    }), flush=True)


if __name__ == "__main__":
    main()
