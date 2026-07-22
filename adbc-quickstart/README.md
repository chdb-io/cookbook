# chDB over ADBC — quickstart (experimental)

> **Experimental / preview.** Behavior and packaging may change before stable.

chDB ships an [ADBC](https://arrow.apache.org/adbc/) driver inside `libchdb`
(and the Python module `_chdb.abi3.so`, which doubles as one). ADBC is a
cross-language standard: once a driver manager has the library path, every
language uses the same `driver=<path>` handle. chDB exports the default
`AdbcDriverInit` symbol, so **no `entrypoint` is needed**.

## Get the library

| User | How |
|---|---|
| **Python** | `pip install chdb-core` — the installed `_chdb.abi3.so` is the driver |
| **Any other language** | download `libchdb.so` / `.dylib` from the chdb-core GitHub release (the Python wheel does not ship a standalone `libchdb`) |

## Python  *(verified)*

```python
import chdb                                    # initializes the module runtime
from adbc_driver_manager import dbapi
import os

driver = os.path.join(os.path.dirname(chdb.__file__), "_chdb.abi3.so")
conn = dbapi.connect(driver=driver, autocommit=True)
with conn.cursor() as cur:
    cur.execute("SELECT number, toString(number) FROM numbers(3)")
    print(cur.fetchall())
    cur.adbc_ingest("t", pa_table, mode="append")   # bulk load an Arrow table
    cur.execute("SELECT ? + count() FROM t", (100,)) # qmark parameters
```

`pip install adbc-driver-manager pyarrow` alongside chdb-core. A plain
`libchdb.so` path works too (no `import chdb` needed in that case).

## C  *(verified)*

```c
// clang -O2 -I<chdb-core>/programs/local example.c -o example && ./example ./libchdb.so
void *h = dlopen(lib_path, RTLD_LOCAL | RTLD_NOW);
// resolve AdbcDriverInit, fill AdbcDriver, then AdbcDatabase/Connection/Statement.
```

See `chdb-core/examples/chdbAdbcTest.c` for the full C-ABI walk.

## Go  *(verified)*

`go get github.com/apache/arrow-adbc/go/adbc` (cgo dlopen; no entrypoint):

```go
var drv drivermgr.Driver
db, _ := drv.NewDatabase(map[string]string{"driver": "/path/libchdb.so"})
cn, _ := db.Open(ctx); stmt, _ := cn.NewStatement()
stmt.SetSqlQuery("SELECT number FROM numbers(3)")
rr, _, _ := stmt.ExecuteQuery(ctx)   // arrow.RecordReader
```

## R  *(verified)*

`install.packages("adbcdrivermanager")`:

```r
library(adbcdrivermanager)
con <- adbc_driver("/path/libchdb.so") |> adbc_database_init() |> adbc_connection_init()
df <- as.data.frame(read_adbc(con, "SELECT number FROM numbers(3)"))
```

## Rust / Java  *(standard driver-manager usage — not run here)*

- **Rust**: `adbc_driver_manager` — `ManagedDriver::load_dynamic_from_filename("/path/libchdb.so", None, ...)`
- **Java**: ADBC-JNI `AdbcDatabase` with the driver path

## rc → stable: what changes

| | rc (now) | stable |
|---|---|---|
| Python install | `pip install chdb-core adbc-driver-manager` + build the driver path by hand | `pip install "chdb[adbc]"`; a helper returns the path |
| Non-Python | download `libchdb.so` from the release | same, plus a possible `dbc install chdb` locator that fetches it for you |
| Status | experimental | marker dropped once stable |

Reproduce the capability matrix behind this driver in `../adbc-foundry-validation/`
(Foundry suite) and `../adbc-validation-report/` (apache/arrow-adbc harness).
