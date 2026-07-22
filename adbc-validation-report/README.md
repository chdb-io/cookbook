# Validating the chDB ADBC driver — a reproducible report

chDB ships an [ADBC](https://arrow.apache.org/adbc/) driver inside `libchdb`
(and the Python module doubles as one). This recipe reproduces its conformance
results under the two validation suites the ADBC ecosystem uses, and compares
the same run against other embedded engines.

## What you'll learn

- How to run the **ADBC Driver Foundry validation suite**
  ([adbc-drivers/validation](https://github.com/adbc-drivers/validation)) —
  the harness that generates the capability matrices on
  [docs.adbc-drivers.org](https://docs.adbc-drivers.org) — against chDB.
- How to run **apache/arrow-adbc's own C++ validation suite** against *any*
  driver loadable through the driver manager, using the generic harness in
  this directory.
- How chDB's results compare to DuckDB and to the upstream-maintained SQLite
  reference driver.

## 1. Foundry validation suite

The driver repo carries its quirks declaration and ClickHouse-dialect case
overrides in-tree (`tests/validation/` in
[chdb-io/chdb-core](https://github.com/chdb-io/chdb-core)), the same way other
Foundry drivers do:

```bash
git clone https://github.com/chdb-io/chdb-core && cd chdb-core
# build libchdb.so first (see the repo README), then:
python -m venv venv && venv/bin/pip install \
    "git+https://github.com/adbc-drivers/validation" pytest pyarrow adbc-driver-manager
cd tests/validation
TZ=UTC CHDB_LIB_PATH=../../libchdb.so ../../venv/bin/python -m pytest tests/
```

Result (macOS arm64, chdb-core `feat/adbc-driver`):

```
214 passed, 27 skipped, 3 xfailed
```

Skips are feature-gated by the quirks declaration (transactions, statistics,
catalogs, ExecuteSchema) or documented engine limits (negative Decimal
scales). The dialect overrides document real type mappings — columns declared
Nullable, binary round-trips as String, TIME/TIMESTAMP as DateTime64 with a
UTC zone — and every override's values are exact round-trips of the upstream
case's input data.

## 2. apache/arrow-adbc C++ validation suite

`adbc_external_validation_test.cc` (in this directory) runs the upstream C++
suite against any driver via the driver manager:

```bash
git clone --depth 1 https://github.com/apache/arrow-adbc /tmp/arrow-adbc
cp adbc_external_validation_test.cc /tmp/arrow-adbc/c/validation/
cat cmake-snippet.txt >> /tmp/arrow-adbc/c/validation/CMakeLists.txt
cmake -S /tmp/arrow-adbc/c -B /tmp/adbc-build \
    -DADBC_BUILD_TESTS=ON -DADBC_DRIVER_MANAGER=ON -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/adbc-build --target adbc-external-validation-test -j8

# chDB
TZ=UTC ADBC_TEST_DRIVER=/path/to/libchdb.so ADBC_TEST_DIALECT=chdb \
    /tmp/adbc-build/validation/adbc-external-validation-test

# DuckDB (download libduckdb from its GitHub releases)
TZ=UTC ADBC_TEST_DRIVER=/path/to/libduckdb.dylib \
    ADBC_TEST_ENTRYPOINT=duckdb_adbc_init ADBC_TEST_DIALECT=duckdb \
    /tmp/adbc-build/validation/adbc-external-validation-test

# SQLite reference driver: upstream's own tuned suite
cmake -S /tmp/arrow-adbc/c -B /tmp/adbc-build -DADBC_DRIVER_SQLITE=ON \
    -DADBC_BUILD_TESTS=ON -DADBC_DRIVER_MANAGER=ON -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/adbc-build --target adbc-driver-sqlite-test -j8
/tmp/adbc-build/driver/sqlite/adbc-driver-sqlite-test
```

## 3. Results (same machine, same suite, 2026-07)

| Driver | Passed | Failed | Skipped |
|---|---|---|---|
| SQLite reference (upstream's own suite, 137 tests) | 121 | 0 | 16 |
| **chDB** (`libchdb`, 100 tests) | **71** | **4** | 25 |
| DuckDB v1.5.4 (100 tests) | 55 | 23 (+3 n/c) | 19 |

Reading the numbers honestly:

- **chDB's 4 failures are documented behavior differences, not defects**:
  bare integer literals type as `UInt8` per ClickHouse rules where the suite
  only accepts int32/int64 (×2); one read expects the whole result in a
  single batch while ClickHouse streams a block per data part (×1); and a new
  statement displaces the connection's active stream by design (×1).
- **DuckDB's failures are real capability gaps** in its ADBC layer as of
  v1.5.4: the GetObjects family, ADBC 1.1 structured error details, prepare
  semantics, binds that return result sets, and six ingest type round-trips
  (binary_view, fixed_size_binary, float16, large_binary, large_string,
  string_view). Three temporal tests are "not comparable" (n/c): the harness
  requires per-driver validation code that this recipe implements for chDB
  only.
- **The SQLite number is the ceiling, not a peer**: it is upstream's own
  driver validated by upstream's own hand-tuned suite.
- The quirks for DuckDB in this harness are best-effort (catalog/schema
  names, transaction flags). If you maintain DuckDB's ADBC layer and spot a
  quirks bug that unfairly fails a test, please open an issue.

## Try next

- Use the driver from any language:
  `connect(driver="/path/to/libchdb.so")` — the default `AdbcDriverInit`
  entrypoint resolves without extra configuration.
- The Foundry suite's per-case overrides and the generator that replays them
  live in `chdb-core/tests/validation/` — useful as a template for validating
  your own ADBC driver.
