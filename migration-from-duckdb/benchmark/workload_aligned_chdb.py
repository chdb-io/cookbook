"""chDB workload — aligned with §2 ordering of migration-from-duckdb.md.

Use-case-driven (§2.1 → §2.7), then baseline analytical SQL, then reference queries.

  Q1  JSON GROUP BY response.status                    §2.1 typed JSON
  Q2  JSON GROUP BY user.tier + avg(latency)           §2.1
  Q3  JSON filter + group                              §2.1
  Q4  DataStore groupby agg (vs pandas baseline)       §2.2 pandas-compatible
  Q5  Vector top-10 cosine                             §2.3 AI-agent retrieval
  Q6  windowFunnel                                     §2.6 funnel/pattern
  Q7  sequenceCount                                    §2.6
  Q8  quantilesTDigest multi-percentile                §2.7 many percentiles
  --- baseline analytical SQL on Parquet ---
  Q9  aggregate count/sum/avg
  Q10 GROUP BY + filter
  Q11 time bucket toStartOfHour
  Q12 approx count distinct uniqHLL12
  Q13 DataFrame round-trip (narrow, 4 cols)
  --- reference queries ---
  Q14 topK pickup zones
  Q15 DataFrame round-trip (wide, 19 cols)
  Q16 persistent storage workflow (load + 5 queries)
  Q17 primary-key range scan
  Q18 Parquet → DataFrame export (single file, full materialisation)
"""

import json, os, time
import chdb
from chdb import session as chdb_session
import datastore as ds_pd
import pandas as pd_real
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
    print(f"\n=== chDB {chdb.__version__} aligned workload ===", flush=True)
    sess = chdb_session.Session()
    sess.query("SET allow_experimental_json_type = 1")
    results = []

    # ---------- §2.1 Typed JSON columns ----------
    sess.query(f"""
        CREATE TABLE events ENGINE = MergeTree ORDER BY id AS
        SELECT assumeNotNull(id) AS id, CAST(payload AS JSON) AS data
        FROM file('{JSON_PARQUET}', 'Parquet')
    """)
    results.append(measure("Q1 JSON GROUP BY response.status", lambda: sess.query("""
        SELECT data.response.status.:String AS status, count(*) AS n
        FROM events GROUP BY status ORDER BY n DESC
    """, "DataFrame")))
    results.append(measure("Q2 JSON GROUP BY user.tier + avg(latency)", lambda: sess.query("""
        SELECT data.user.tier.:String AS tier,
               avg(data.response.latency_ms.:Float64) AS avg_lat,
               count(*) AS n
        FROM events GROUP BY tier
    """, "DataFrame")))
    results.append(measure("Q3 JSON filter + group", lambda: sess.query("""
        SELECT data.tool.:String AS tool, count(*) AS n
        FROM events
        WHERE data.user.region.:String = 'us-east-1'
        GROUP BY tool ORDER BY n DESC
    """, "DataFrame")))

    # ---------- §2.2 DataStore (pandas-compatible) ----------
    df_ds = ds_pd.read_parquet(TAXI_SINGLE, columns=[
        "passenger_count", "fare_amount", "tip_amount", "trip_distance"
    ])
    results.append(measure("Q4 DataStore groupby agg", lambda:
        df_ds.groupby("passenger_count").agg(
            avg_fare=("fare_amount", "mean"),
            sum_tip=("tip_amount", "sum"),
            avg_dist=("trip_distance", "mean"),
            n=("fare_amount", "count"),
        ).to_pandas()
    ))

    # ---------- §2.3 AI-agent retrieval (vector) ----------
    sess.query(f"""
        CREATE TABLE vecs ENGINE = MergeTree ORDER BY id AS
        SELECT assumeNotNull(id) AS id,
               arrayMap(x -> toFloat32(assumeNotNull(x)), emb) AS emb
        FROM file('{VEC_PARQUET}', 'Parquet')
    """)
    qv = "[" + ",".join(f"{x:.6f}" for x in QUERY_VEC) + "]"
    results.append(measure("Q5 vector top-10 cosine", lambda: sess.query(f"""
        SELECT id, cosineDistance(emb, {qv}) AS d
        FROM vecs ORDER BY d ASC LIMIT 10
    """, "DataFrame")))

    # ---------- §2.6 Funnel / pattern (taxi 6-month MergeTree) ----------
    sess.query(f"""
        CREATE TABLE trips ENGINE = MergeTree ORDER BY tpep_pickup_datetime AS
        SELECT PULocationID,
               assumeNotNull(toDateTime(tpep_pickup_datetime)) AS tpep_pickup_datetime,
               assumeNotNull(fare_amount)                     AS fare_amount,
               ifNull(Airport_fee, 0)                          AS Airport_fee
        FROM file('{TAXI_GLOB}', 'Parquet')
    """)
    results.append(measure("Q6 windowFunnel", lambda: sess.query("""
        SELECT funnel_level, count(*) AS zones FROM (
            SELECT PULocationID AS zone,
                   windowFunnel(3600)(
                       tpep_pickup_datetime,
                       fare_amount < 15, fare_amount > 50, Airport_fee > 0
                   ) AS funnel_level
            FROM trips
            WHERE fare_amount < 15 OR fare_amount > 50 OR Airport_fee > 0
            GROUP BY zone
        ) WHERE funnel_level > 0 GROUP BY funnel_level ORDER BY funnel_level
    """, "DataFrame")))
    results.append(measure("Q7 sequenceCount", lambda: sess.query("""
        SELECT sum(seq_count) AS sequence_count FROM (
            SELECT PULocationID,
                   sequenceCount('(?1)(?2)(?3)')(
                       tpep_pickup_datetime,
                       fare_amount < 15, fare_amount > 50, fare_amount > 70
                   ) AS seq_count
            FROM trips GROUP BY PULocationID
        )
    """, "DataFrame")))

    # ---------- §2.7 Many percentiles ----------
    results.append(measure("Q8 quantilesTDigest p50/p95/p99", lambda: sess.query(f"""
        SELECT quantilesTDigest(0.5, 0.95, 0.99)(fare_amount) AS pcts
        FROM file('{TAXI_GLOB}', 'Parquet') WHERE fare_amount > 0
    """, "DataFrame")))

    # ---------- Baseline analytical SQL on Parquet ----------
    results.append(measure("Q9 aggregate", lambda: sess.query(f"""
        SELECT count(*) AS trips, sum(total_amount) AS revenue,
               avg(trip_distance) AS avg_distance
        FROM file('{TAXI_GLOB}', 'Parquet')
    """, "DataFrame")))
    results.append(measure("Q10 group+filter", lambda: sess.query(f"""
        SELECT payment_type, count(*) AS trips, avg(tip_amount) AS avg_tip
        FROM file('{TAXI_GLOB}', 'Parquet')
        WHERE fare_amount > 50 GROUP BY payment_type ORDER BY trips DESC
    """, "DataFrame")))
    results.append(measure("Q11 time bucket toStartOfHour", lambda: sess.query(f"""
        SELECT toStartOfHour(tpep_pickup_datetime) AS hour, count(*) AS trips
        FROM file('{TAXI_GLOB}', 'Parquet')
        GROUP BY hour ORDER BY hour LIMIT 24
    """, "DataFrame")))
    results.append(measure("Q12 approx count distinct uniqHLL12", lambda: sess.query(f"""
        SELECT uniqHLL12(PULocationID) AS unique_pickups,
               uniqHLL12(DOLocationID) AS unique_dropoffs
        FROM file('{TAXI_GLOB}', 'Parquet')
    """, "DataFrame")))

    df_narrow = pd_real.read_parquet(TAXI_SINGLE, columns=[
        "passenger_count", "trip_distance", "fare_amount", "tip_amount"
    ])
    results.append(measure("Q13 DataFrame roundtrip (narrow)", lambda: sess.query("""
        SELECT passenger_count, count(*) AS n,
               avg(fare_amount) AS avg_fare, avg(tip_amount) AS avg_tip
        FROM Python(df_narrow)
        WHERE trip_distance BETWEEN 1 AND 20
        GROUP BY passenger_count ORDER BY n DESC
    """, "DataFrame")))

    # ---------- Reference queries ----------
    results.append(measure("Q14 topK pickup zones", lambda: sess.query(f"""
        SELECT topK(10)(PULocationID) FROM file('{TAXI_GLOB}', 'Parquet')
    """, "DataFrame")))

    df_wide = pd_real.read_parquet(TAXI_SINGLE)
    results.append(measure("Q15 DataFrame roundtrip (wide, 19 cols)", lambda: sess.query("""
        SELECT VendorID, count(*) AS trips,
               avg(trip_distance) AS avg_dist, sum(fare_amount) AS total_fare,
               sum(tip_amount) AS total_tip, avg(passenger_count) AS avg_pax
        FROM Python(df_wide) WHERE fare_amount > 0
        GROUP BY VendorID ORDER BY trips DESC
    """, "DataFrame")))

    def q16():
        sess.query("DROP TABLE IF EXISTS trips_perf_test")
        sess.query(f"""
            CREATE TABLE trips_perf_test
            ENGINE = MergeTree ORDER BY tpep_pickup_datetime AS
            SELECT VendorID,
                   assumeNotNull(toDateTime(tpep_pickup_datetime)) AS tpep_pickup_datetime,
                   PULocationID, DOLocationID, passenger_count,
                   trip_distance, fare_amount, tip_amount, total_amount
            FROM file('{TAXI_GLOB}', 'Parquet')
        """)
        return [
            sess.query("SELECT count(*) FROM trips_perf_test", "CSV"),
            sess.query("SELECT PULocationID, count(*) c FROM trips_perf_test GROUP BY PULocationID ORDER BY c DESC LIMIT 5", "CSV"),
            sess.query("SELECT VendorID, avg(fare_amount) FROM trips_perf_test GROUP BY VendorID", "CSV"),
            sess.query("SELECT toStartOfDay(tpep_pickup_datetime) d, count(*) FROM trips_perf_test GROUP BY d ORDER BY d LIMIT 10", "CSV"),
            sess.query("SELECT count(*) FROM trips_perf_test WHERE total_amount > 100", "CSV"),
        ]
    results.append(measure("Q16 persistent storage (load+5q)", q16))

    results.append(measure("Q17 PK range scan", lambda: sess.query("""
        SELECT count(*), avg(fare_amount), avg(tip_amount)
        FROM trips_perf_test
        WHERE tpep_pickup_datetime BETWEEN '2024-03-15 16:00:00' AND '2024-03-15 20:00:00'
    """, "DataFrame")))

    results.append(measure("Q18 Parquet → DataFrame export", lambda: sess.query(f"""
        SELECT * FROM file('{TAXI_SINGLE}', 'Parquet')
    """, "DataFrame")))

    print("\n__RESULTS_JSON__", json.dumps({
        "engine": "chdb", "version": chdb.__version__, "queries": results,
    }), flush=True)


if __name__ == "__main__":
    main()
