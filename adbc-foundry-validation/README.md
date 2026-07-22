# chDB ADBC driver — validation & capability matrix

This directory validates the chDB ADBC driver with the
[adbc-drivers/validation](https://github.com/adbc-drivers/validation) suite —
the same harness the ADBC Driver Foundry uses to generate its per-driver
capability matrices.

## Running

```bash
# build libchdb.so first (see the repo README), then:
python -m venv venv
venv/bin/pip install "git+https://github.com/adbc-drivers/validation" pytest adbc-driver-manager pyarrow
cd tests/validation
TZ=UTC CHDB_LIB_PATH=../../libchdb.so ../../venv/bin/python -m pytest tests/
```

Latest local result (macOS arm64): **215 passed, 27 skipped, 3 xfailed, 0 failed**.
The suite also runs in every wheel CI matrix on Python 3.13 (the framework
requires >=3.13), pinned to a fixed framework commit.

## Capability matrix

Every skip / xfail / dialect override falls into one of three buckets. This
list is the source of truth for what is a **ClickHouse hard limit** versus a
**driver choice / not-yet-implemented** — so the green suite is not mistaken
for "everything works".

### Supported (verified green)

- Query execution, prepared statements, positional `?` parameter binding
- Bulk ingestion: create / append / create_append / replace, into a target
  db_schema
- Metadata: GetObjects (all depths), GetTableSchema, GetTableTypes, GetInfo,
  GetOption(current db_schema), GetParameterSchema
- Type round-trips for the integer / float / decimal / string / date / time /
  timestamp families

### ClickHouse hard limits (not a driver defect)

| Area | Limit |
|---|---|
| Decimal | Negative scales unsupported (`ARGUMENT_OUT_OF_BOUND`) — case skipped |
| Nullability | Columns are non-nullable by default; SQL `INT`/`VARCHAR` must be declared `Nullable(...)` to store NULL (dialect override on the sample table) |
| Date32 | Range is 1900-01-01 .. 2299-12-31; out-of-range case values are clamped |
| Catalogs | ClickHouse has databases only, no catalog layer — target-catalog ingest and catalog metadata are N/A |
| Temporary tables | No session-temp-table namespace matching the suite's model |
| Dictionary | The engine's Arrow reader rejects dictionaries with duplicate values |
| Arrays | `Array(T)` columns cannot be NULL, so nullable-list cases don't apply |

### Driver choices / not implemented (first release)

| Feature | Status | Why |
|---|---|---|
| Transactions | Not supported | The embedded engine is autocommit-only |
| Statement.ExecuteSchema | Not implemented | — |
| Connection.GetStatistics | Not implemented | — |
| Statement/Connection Cancel | Not implemented | Deferred; the C API can cancel a stream, wiring is future work |
| Partitioned execution | Not implemented | Single-process embedded engine |

### Lossy / dialect type mappings (documented, values still round-trip exactly)

| Arrow input | Stored / returned as | Note |
|---|---|---|
| binary, large_binary, binary_view, fixed_size_binary | `String` (utf8 on read) | payload bytes preserved; Arrow *type* is not (binary→utf8). Non-UTF-8 payloads are out of scope for the round-trip cases |
| large_string, string_view | `String` | — |
| float16 | `Float32` | engine has no half-float |
| TIME(n) / TIMESTAMP(n) | `DateTime64(n[, tz])` | epoch values preserved |

## How the dialect overrides are generated

`gen_overrides.py` replays each upstream bind/ingest case through the driver
and records the **actual** round-trip schema and values as the override's
expected parts. The recorded *values* are exact round-trips of the upstream
case's input (NULLs, epochs, and numbers compare equal); only the *type*
mapping (e.g. binary→String) reflects the engine's behavior. This documents
the mapping rather than hiding a defect — anything that is a real gap is listed
under the buckets above, not silently overridden.

Regenerate after an engine baseline bump (see the sync playbook), then review
the diff:

```bash
cd tests/validation
CHDB_LIB_PATH=../../libchdb.so python gen_overrides.py
git diff queries/
```

The `tests/engine_version.py` pin must also be bumped on a baseline sync; a
tripwire test in `tests/test_adbc_driver.py` fails in CI until it matches the
built engine.
