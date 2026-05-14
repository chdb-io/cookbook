# Migration benchmark — deep dive

Companion to the [Migrating from DuckDB to chDB](https://clickhouse.com/docs/chdb/guides/migration-from-duckdb) guide. The guide's §5 shows machine, dataset, scenarios, results, and a one-paragraph summary. This file holds the parts that don't fit a guide-style flow: ingest-path methodology, storage-engine trade-off analysis, per-case studies with side-by-side SQL, and the DataFrame round-trip operation matrix.

For the runnable code and reproduction steps, see [README.md](README.md). The canonical results live in [`benchmark/results_aligned.json`](benchmark/results_aligned.json).

---

## How the data is loaded (ingest-path methodology)

The JSON workload (Q1–Q3) is loaded apples-to-apples but **stored differently by design**, because the storage strategy is exactly what §2.1 of the guide compares:

- **chDB**: `CREATE TABLE events (data JSON) ENGINE = Memory` and `INSERT INTO events SELECT … FROM file('events.jsonl', 'JSONEachRow')` — `JSON` type extracts typed sub-columns at load time, so `data.user.tier.:String` later reads a packed column.
- **DuckDB**: `CREATE TABLE events AS SELECT … FROM read_json('events.jsonl')` with the column typed as `JSON` (DuckDB v1.2+ typed `JSON`). Path access resolves against the encoded value at query time.

Both engines ingest the same JSONL file and use the typed `JSON` type provided by the engine. The Q1–Q3 query numbers are **query time only** (ingest is excluded from each engine's timer). chDB amortises a per-path extraction cost during ingest that DuckDB does not pay, but DuckDB then pays per-row at query time. For workloads that ingest once and query many times (the agent-event pattern), this is the architectural trade we want to measure. For workloads that ingest once and query once, it tilts a few hundred milliseconds toward DuckDB — quantify this on your own data if it matters.

The analytical-SQL workload (Q9–Q15) reads the same six Parquet files on both engines.

---

## Storage-engine trade-off (Q17 / Q18)

Two queries in the workload measure a chDB MergeTree storage-engine design choice rather than query-kernel performance, so they are reported separately from the guide's main results table:

- **Q17 — persistent storage workflow** (`CREATE TABLE … AS SELECT` + 5 follow-up queries on the persisted table): DuckDB 129 ms vs chDB 1854 ms. chDB's `MergeTree` builds a sorted index at write time — an upfront cost that amortises as you run more queries against the same data, but is heavy when you only run 5. For one-shot ETL-then-query workflows, DuckDB's single-file write is the right call.
- **Q18 — PK range scan** on a sorted timestamp column: DuckDB 0.4 ms vs chDB 2.9 ms (absolute gap 2.5 ms). At this scale, MergeTree's primary-key bookkeeping cost dominates a query that touches only a few rows; the gap is real but small in absolute terms.

These are signposts that one-shot ETL-then-query workflows should stay on DuckDB; they are not query-engine performance gaps.

---

## Case studies — why chDB wins each workload

### Case A — Typed JSON sub-columns: path access compiles to a column read (Q1, 8.4× faster) — §2.1

Both engines store the column as `JSON`-typed (DuckDB v1.2+ ships a typed `JSON` type too, not the older "stored as text" representation) and accept the same path-access syntax. The architectural difference is in *when* the sub-column extraction happens — chDB at load time, DuckDB at query time — and the cost shows immediately.

**chDB** — path notation with a typed-subcolumn suffix:

```sql
SELECT data.response.status.:String AS status, count(*) AS n
FROM events GROUP BY status ORDER BY n DESC
```

The `.:String` suffix points at the *typed* sub-column chDB extracted at load time. The read is O(1) into a packed column with its own min/max and compression — no per-row JSON parse.

**DuckDB** — same path syntax against a `JSON`-typed column:

```sql
SELECT data.response.status AS status, count(*) AS n
FROM events GROUP BY status ORDER BY n DESC
```

DuckDB's typed `JSON` resolves the path against the encoded value at query time (rather than reading a pre-extracted typed sub-column). The result is correct; the per-row cost is higher.

On 1 M synthetic agent-event records: **DuckDB 35 ms → chDB 4 ms** (8.4×). Two-path and filter-plus-group variants (Q2, Q3) show a 3.0–3.8× win even with arithmetic added. For agent pipelines that accumulate JSON tool-output context and slice it repeatedly, this is the largest standalone win in the whole benchmark.

### Case B — DataStore: a drop-in pandas API that DuckDB has no equivalent for — §2.2

DataStore is what makes chDB a viable replacement for an agent codebase already written in pandas. Same source, one import-line change:

```python
import datastore as pd                    # the only line that changes
df = pd.read_parquet("yellow_tripdata_2024-01.parquet",
                     columns=["passenger_count", "fare_amount", "tip_amount", "trip_distance"])
df.groupby("passenger_count").agg(
    avg_fare=("fare_amount", "mean"),
    sum_tip=("tip_amount", "sum"),
    avg_dist=("trip_distance", "mean"),
    n=("fare_amount", "count"),
).to_pandas()                              # materialise the lazy plan
```

DataStore compiles the expression to ClickHouse SQL via the chDB engine; the lazy `.to_pandas()` call materialises the result. Filters, group-bys, joins, time bucketing, window functions, string and datetime accessors — every common pandas idiom keeps the same shape.

**Coverage**: ~300 pandas-shaped methods (209 `DataFrame` + 56 `.str` + 42+ `.dt` accessors), plus 334 ClickHouse SQL functions surfaced as DataStore methods for things pandas does not have native names for. For agent and notebook code that already speaks pandas, the migration is one `import` line and the rest of the codebase stays untouched. **DuckDB has no equivalent pandas-method surface** — DuckDB's Python API exposes the SQL surface (via replacement scan you can `duckdb.sql("SELECT … FROM pdf")` directly against a DataFrame, no `register()` needed), but a codebase written as `df.filter(...).groupby(...).agg(...)` chained pandas calls has to be rewritten in SQL. With chDB the chained syntax stays.

### Case C — Vector search: integration over raw scan speed (Q5) — §2.3

A production agent's retrieval path is rarely just cosine — it looks like:

```
JSON typed metadata filter  →  vector top-K cosine  →  SQL JOIN session history  →  return DataFrame
```

**chDB runs the whole pipeline as one SQL statement against one in-process engine:** vectors in `Array(Float32)`, session memory in `chdb.session.Session('path')`, JSON metadata as typed sub-columns (Case A), analytical SQL on top. DuckDB has the raw distance kernel in core (`array_cosine_distance`) so no extension is required for linear-scan retrieval, but the surrounding pieces are not co-located — no native session abstraction (external SQLite / Postgres / Redis), and HNSW indexing requires `INSTALL/LOAD vss`. The retrieval pipeline still pays the 3–8× JSON metadata filter gap (Case A) on DuckDB.

For the isolated kernel — `SELECT id, cosineDistance(emb, [...]) ORDER BY d LIMIT 10` over 100 K × 384-d random unit vectors, no index — DuckDB 35 ms vs chDB 64 ms. A real but operationally small 30 ms gap; both engines well under the 100 ms interactive threshold. With approximate-NN indexes (chDB vector skip-indexes, DuckDB `vss`) the linear-scan delta is irrelevant. **The interesting comparison for agents is the workflow, not the kernel.**

### Case D — `windowFunnel`: funnel analysis in one line (Q6, 2.61× faster) — §2.8

For each pickup zone, find the longest matching prefix of: `low-fare (<$15)` → `high-fare (>$50)` → `airport trip` within a one-hour window.

**chDB** — one aggregate:

```sql
SELECT PULocationID,
       windowFunnel(3600)(tpep_pickup_datetime,
           fare_amount < 15, fare_amount > 50, Airport_fee > 0) AS funnel_level
FROM trips GROUP BY PULocationID
```

**DuckDB** — no native funnel function, so a CTE chain with `LAG(step,1) / LAG(ts,1) / LAG(step,2) / LAG(ts,2)` over the event stream plus `CASE WHEN step=3 AND prev1=2 AND prev2=1 AND EPOCH(ts-prev2_ts) < 3600 THEN 3 …`. Full DuckDB version is in [`workload_aligned_duckdb.py`](benchmark/workload_aligned_duckdb.py) Q6 — roughly thirty lines vs chDB's six.

**274 ms → 105 ms on 18 M rows** (2.61×). The DuckDB version peaks at ~2.0 GB RSS (sorted intermediates for `LAG`); chDB peaks at 1.4 GB.

### Case E — `sequenceCount`: pattern matching in a single aggregate (Q7, 4.54× faster) — §2.8

Count how many times the sequence `low-fare → high-fare → very-high-fare` occurs per pickup zone (then sum).

**chDB**

```sql
SELECT PULocationID,
       sequenceCount('(?1)(?2)(?3)')(
           tpep_pickup_datetime,
           fare_amount < 15,
           fare_amount > 50,
           fare_amount > 70
       ) AS seq_count
FROM trips
GROUP BY PULocationID
```

The `'(?1)(?2)(?3)'` pattern is a regex-like expression over the event predicates — chDB has the matching engine built in. `sequenceMatch` (boolean variant) and per-window variants are right next to it.

**DuckDB** needs essentially the same LAG-CTE structure as the funnel case in Case D, plus a `GROUP BY` to collapse counts. **230 ms → 51 ms** on 18 M rows (4.54×), and the chDB version is half a dozen lines vs roughly thirty in DuckDB.

### Case F — `quantilesTDigest`: many percentiles, one sketch (Q8, 2.35× faster) — §2.9

P50, P95, and P99 of fare amount, ignoring zero-amount rides — both engines support a single-sketch multi-percentile API.

**chDB**

```sql
SELECT quantilesTDigest(0.5, 0.95, 0.99)(fare_amount) AS pcts
FROM file('yellow_tripdata_2024-*.parquet', 'Parquet')
WHERE fare_amount > 0
```

**DuckDB** — list-form `approx_quantile` (single scan, single TDigest sketch — apples-to-apples with chDB):

```sql
SELECT approx_quantile(fare_amount, [0.5, 0.95, 0.99]) AS pcts
FROM read_parquet(...)
WHERE fare_amount > 0
```

Both queries produce one array `[p50, p95, p99]` from a single TDigest sketch over the data. The implementation difference shows in raw throughput: **64 ms → 27 ms** on 18 M rows (chDB 2.35×). The percentile-dashboard pattern is everywhere (SLO reports, p99-latency analytics), so even a 2× edge on the sketch kernel adds up at fleet scale.

The chDB family is also wider — `quantilesExact`, `quantilesGK`, `quantilesBFloat16Weighted` — when you need a different precision / memory trade-off than TDigest, the API shape stays identical.

### Case G — Parquet → DataFrame export: zero-copy materialisation (Q16, 1.61× faster cold / 2.99× warm)

Load a Parquet file and return the full result as a pandas DataFrame — the *output* path. This is the operation behind chDB's published "24% faster than DuckDB on DataFrame export" claim from the [zero-copy blog](https://clickhouse.com/blog/chdb-journey-to-zero-copy) (January 2026, ClickBench hits, 1 M rows). On our 3 M-row × 19-col NYC TLC file: cold 392 ms → 244 ms (1.61×, 38 % reduction); warm 325 ms → 108 ms (2.99×, 67 %). Both meet or exceed the blog. Mechanism: chDB's `__arrow_c_stream__` zero-copy SIMD path materialises columns directly into NumPy with no intermediate Arrow → pandas copy.

**Important — not the same as `Python(df)`.** Q16 is the **output** path. Q13 / Q15's `Python(df)` table function is the **input** path (existing pandas DataFrame → SQL → DataFrame) and goes through different machinery. Performance there is operation-dependent — see "DataFrame round-trip — input depends on the operation" below.

---

## Note — §2.5's connectivity advantage isn't a benchmark line

The 18 queries measure kernel performance on fixed input shapes. They do not measure §2.5 — the ~80-format, 12+-connector, three-streaming-engine in-core surface — which shows up in **deployment shape**, not milliseconds: no `INSTALL/LOAD` chain, no MongoDB / Redis client to pip-install into the agent's runtime, no separate Kafka / RabbitMQ / NATS consumer process, no Python-side decode before SQL on Protobuf / Avro / MsgPack input. For an agent whose data is already a clean Parquet file, this disappears; for one whose data is the firehose the surrounding system emits, it is the largest single operational difference, and invisible in any single-engine query timing.

---

## DataFrame round-trip — input depends on the operation

Q16 (output path) and Q13 / Q15 (input path) go through different machinery; the input-path result is operation-dependent, not a flat win for either engine:

| Path | Operation | Result |
|---|---|---|
| Output — Parquet → DataFrame (Q16) | full file export | **chDB 1.61× cold, 2.99× warm** |
| Input, warm in-process, 10 M rows | `COUNT(*)` | **chDB 1.4×** |
| Input, warm in-process, 10 M rows | filter + `COUNT` | **chDB 1.1×** |
| Input, warm in-process, 10 M rows | `COUNT(*)` on wide (60-col) DF | **chDB 2.1×** |
| Input, warm in-process, 10 M rows | `GROUP BY` | DuckDB 1.0–1.3× |
| Input, cold subprocess (Q13 / Q15) | `GROUP BY` | DuckDB 1.6–2× (mostly chDB engine-init cost) |

Op type matters more than engine choice — chDB wins lightweight aggregates outright; DuckDB has a narrow advantage on `GROUP BY`. The cold-subprocess penalty is real for short-lived Lambda-style invocations but is **not** a steady-state `Python(df)`-vs-`register()` difference.

Run `benchmark/bench_input_path_scale.py` and `benchmark/bench_input_path_variants.py` to reproduce these numbers on your own hardware.
