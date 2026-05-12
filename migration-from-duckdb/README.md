# Migrating from DuckDB to chDB — reproducibility code

Runnable companion to the [Migrating from DuckDB to chDB](https://clickhouse.com/docs/chdb/guides/migration-from-duckdb)
guide. The prose lives at the docs site; this directory holds the static
migration analyzer and the 18-query benchmark referenced from §5 of the guide.

## What's here

```
migration-from-duckdb/
├── README.md                      # this file
├── migrate.py                     # static DuckDB→chDB API analyzer
└── benchmark/
    ├── workload_aligned_duckdb.py # 18-query DuckDB workload
    ├── workload_aligned_chdb.py   # same 18 queries, migrated to chDB
    ├── run_aligned.py             # 3-run-median benchmark runner
    ├── gen_data.py                # synthesises JSON events + embedding vectors
    ├── results_aligned.json       # canonical run on Apple M5 Max / chDB 4.1.6 / DuckDB 1.4.4
    ├── bench_input_path_scale.py  # supplementary: input path vs scale (3M / 10M / 18M)
    └── bench_input_path_variants.py # supplementary: input path vs op-type and width
```

## Reproduce in five minutes

```bash
# 1. Environment
python3 -m venv .venv && source .venv/bin/activate
pip install duckdb chdb pandas pyarrow psutil

# 2. Fetch the analytical-SQL dataset (~326 MB, six monthly NYC TLC Yellow Taxi files)
cd benchmark
for m in 01 02 03 04 05 06; do
    curl -sL -o "yellow_tripdata_2024-$m.parquet" \
        "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-$m.parquet"
done
cd ..

# 3. Generate the JSON + vector datasets for the agent-domain benchmarks (~160 MB)
python benchmark/gen_data.py

# 4. Run the full 18-query × 3-iteration × 2-engine benchmark
python benchmark/run_aligned.py
```

The fresh report writes to `benchmark/results_aligned.json`. The version committed
here is the canonical run on the reference hardware; expect different absolute
timings on different machines and roughly stable relative speedups.

## Using `migrate.py` on your own DuckDB project

```bash
python migrate.py /path/to/your/project              # report touch points
python migrate.py /path/to/your/project --apply      # rewrite simple cases (backs up to *.bak)
python migrate.py /path/to/your/project --dialect-only
```

`migrate.py` flags the mechanical replacements covered in §4 of the guide
(import, connect, `read_parquet`, `date_trunc`, `approx_count_distinct`,
`register(...)`, etc.) plus dialect-review items that need human judgment
(`PIVOT`, `INSTALL`, `STRUCT`, `CREATE INDEX`).

## Supplementary input-path benchmarks

The two `bench_input_path_*.py` scripts back up the §5 "Reading the numbers
honestly" claim that DataFrame *input* via `Python(df)` is operation-dependent
(not a flat DuckDB win as Q13 / Q15 alone might suggest).

```bash
# 1. Vary row count, fix the operation
python benchmark/bench_input_path_scale.py
#    3 M / 10 M / 18 M rows on the NYC TLC base, GROUP BY,
#    warm in-process iterations.

# 2. Vary the operation, fix the scale (10 M rows)
python benchmark/bench_input_path_variants.py
#    V1 COUNT(*) / V2 filter + COUNT / V3 GROUP BY (19 cols),
#    V4 GROUP BY / V5 COUNT(*) on a synthetically widened 60-col DataFrame.
```

Headline result (Apple M5 Max, chDB 4.1.6 vs DuckDB 1.4.4, warm in-process):

- chDB wins on lightweight aggregates: `COUNT(*)` 1.4×, `filter + COUNT` 1.1×,
  wide-DataFrame `COUNT(*)` 2.1×.
- DuckDB wins on `GROUP BY` by 1.0–1.3×; gap is stable across 3 M / 10 M / 18 M.
- Consistent with the chDB v4 zero-copy blog's "7:3 advantage across 14 ops at
  10 M rows" claim — op type matters more than engine choice.

## Reference hardware

The canonical run was produced with:

- Apple M5 Max (18 cores: 6P + 12E), 36 GB RAM, macOS 26.4.1
- Python 3.9.6
- `chdb` 4.1.6, `chdb-core` 26.3.0 (ClickHouse 26.3.9.1-lts)
- `duckdb` 1.4.4

## License

Apache 2.0 — see [LICENSE](../LICENSE).

## Related

- Canonical guide: <https://clickhouse.com/docs/chdb/guides/migration-from-duckdb>
- Main chDB repository: <https://github.com/chdb-io/chdb>
- LLM-friendly index: <https://clickhouse.com/docs/chdb/llms.txt>
