# Agent memory that survives the machine

An agent learns a project rule during a deployment task:

> Deploy `checkout-api` to `eu-west-1`.

It uses the rule, finishes the task, and exits. The next job starts in CI on another machine. The repository is there, but the first agent's local database is not.

[chDB Durable](https://github.com/chdb-io/chdb/blob/main/docs/durable/index.mdx) makes that local database recoverable on another machine without adding a cloud database server. chDB still runs inside the agent process, and recall queries read a working copy on the current machine. Object storage holds the committed checkpoints, WAL segments, and coordination metadata. When another process opens the same object, Durable restores the database locally before returning it.

This recipe uses a small data model based on [ClickMem](https://github.com/auxten/clickmem). It records one decision, closes the database, opens it again, recalls the decision, revises it, and prints both versions.

![Local chDB is the working copy; object storage holds the recoverable state](assets/chdb-durable-architecture.svg)

## Run the story

Python Durable is available in chDB 4.4.0. Install it and run the included script:

```bash
python -m pip install "chdb[durable]>=4.4.0"
python agent_memory.py demo
```

The demo uses `local:/tmp/chdb-durable-agent-memory` and creates a new object on every run. It closes and reopens that object between steps, which exercises checkpoint restore and WAL replay on one host.

The output ends with the revised memory and its history:

```text
{"content":"Deploy checkout-api to eu-central-1.", ...}

┌─version─┬─op─────┬─content──────────────────────────────────┐
│       1 │ expand │ Deploy checkout-api to eu-west-1.       │
│       2 │ revise │ Deploy checkout-api to eu-central-1.    │
└─────────┴────────┴──────────────────────────────────────────┘
```

To recover the same object on another machine, configure both machines with the same object-storage namespace and object ID:

```bash
export CHDB_DURABLE_URL=s3://my-agent-state/agent-memory
export AWS_REGION=eu-west-1
export CHDB_DURABLE_OBJECT_ID=acme-checkout-api
```

On machine A, create the tables and commit the first memory:

```bash
python agent_memory.py init
python agent_memory.py remember
```

Wait for `remember` to print `remembered after durable flush`. The checkpoint and WAL are now committed. On machine B, restore the object, recall the memory, revise it, and inspect its history:

```bash
python agent_memory.py recall
python agent_memory.py revise
python agent_memory.py history
```

Machine B must use the same three environment variables and the same AWS identity or an identity with access to that prefix. Keep credentials out of the namespace URL. For MinIO, R2, or another S3-compatible provider, set `CHDB_DURABLE_S3_ENDPOINT`.

The complete example is in [`agent_memory.py`](agent_memory.py).

## How Durable maps to this example

A namespace names a storage root. An object is one complete chDB database below that root.

```text
s3://my-agent-state/agent-memory   namespace
└── acme-checkout-api             object: one database, one writer at a time
```

Object IDs are one flat path segment, so this recipe uses `acme-checkout-api`. Put hierarchy such as organization and environment in the namespace prefix.

The API has six operations to remember:

| Call | What has happened when it returns |
|---|---|
| `open()` | The latest checkpoint has been restored and committed WAL segments have been replayed |
| `query()` | One read-only statement ran against the local working copy |
| `execute()` | One mutation ran locally and entered the in-memory WAL buffer |
| `flush()` | The buffered statements and the new head have been committed to object storage |
| `checkpoint()` | A full database snapshot has become the new base; the committed WAL list is empty |
| `close()` | Remaining writes were flushed, the writer lease was released, and local scratch data was cleaned up |

The distinction between `execute()` and `flush()` affects user-visible behavior. If an agent says "I will remember that" after `execute()`, the statement can still disappear with the host. The tool should return success after `flush()`:

```python
obj.execute("INSERT INTO memories VALUES (...)")
wal_key = obj.flush()
print(f"remembered after durable flush: {wal_key}")
```

`checkpoint()` is also a durability boundary. It captures the current database, including statements still in the buffer.

## The memory tables

ClickMem separates reviewed memory from the material that produced it. The current implementation has six tables:

| Table | Contents |
|---|---|
| `memories` | The current version of each belief |
| `memory_history` | An immutable row for every expand, revise, contract, pin, and resolve operation |
| `projects` | Project metadata and permitted cross-project recall |
| `blacklist` | Patterns that must not enter memory |
| `raw_transcripts` | Cold evidence that is searchable but is never recalled automatically |
| `events` | Mutation, integration, and recall audit events |

ClickMem does not use a separate `conflicts` table. A conflicting memory has `status='conflicted'` and IDs in `conflict_with`. Recall scoring is calculated when requested. The `events` table stores `recall.run` records with a query preview and the IDs that matched.

This recipe keeps the four tables needed for the example: `memories`, `memory_history`, `raw_transcripts`, and `events`.

<details>
<summary>Show the SQL schema</summary>

```sql
CREATE TABLE IF NOT EXISTS memories (
    id            String,
    content       String CODEC(ZSTD(3)),
    kind          LowCardinality(String),
    source        LowCardinality(String),
    source_ref    String,
    project_id    String,
    privacy       LowCardinality(String),
    tags          Array(String),
    embedding     Array(Float32),
    status        LowCardinality(String),
    pinned        UInt8,
    revises_id    String,
    conflict_with Array(String),
    created_at    DateTime64(3, 'UTC'),
    updated_at    DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY id;

CREATE TABLE IF NOT EXISTS memory_history (
    memory_id String,
    version   UInt32,
    op        LowCardinality(String),
    content   String CODEC(ZSTD(3)),
    edited_by String,
    edited_at DateTime64(3, 'UTC'),
    prev_id   String,
    note      String
)
ENGINE = MergeTree
ORDER BY (memory_id, version, edited_at);

CREATE TABLE IF NOT EXISTS raw_transcripts (
    id         String,
    session_id String,
    agent      LowCardinality(String),
    project_id String,
    role       LowCardinality(String),
    text       String CODEC(ZSTD(3)),
    text_hash  String,
    meta_json  String CODEC(ZSTD(3)),
    created_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree
ORDER BY (session_id, created_at, id);

CREATE TABLE IF NOT EXISTS events (
    id           String,
    kind         LowCardinality(String),
    agent        LowCardinality(String),
    project_id   String,
    memory_id    String,
    message      String,
    payload_json String CODEC(ZSTD(3)),
    created_at   DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(created_at)
ORDER BY (created_at, kind, id)
TTL toDateTime(created_at) + INTERVAL 30 DAY;
```

</details>

The tables have no `DEFAULT now()` expressions. Durable V1 stores mutation statements in the WAL and runs them again during recovery. The application therefore generates each ID and timestamp once, then writes the value as a SQL literal.

## Remembering one decision

The `remember` command performs two distinct actions. It first saves the transcript as evidence. It then promotes the reviewed decision into `memories` and records version 1 in `memory_history`.

```python
transcript = "User confirmed: deploy checkout-api to eu-west-1."
created_at = utc_now()  # generated once; the SQL contains the resulting literal

obj.execute("INSERT INTO raw_transcripts ...")
obj.execute("INSERT INTO memories ...")
obj.execute("INSERT INTO memory_history ...")
obj.execute("INSERT INTO events ...")
obj.flush()
```

Those four statements are committed in order in one WAL segment. They are not a multi-statement SQL transaction. An application that needs a stronger invariant should express it in one deterministic statement or validate the group during recovery.

Raw transcripts never become memory automatically. A temporary request, an error message, or an untrusted tool result can remain searchable evidence without changing future agent behavior.

## Recovering and recalling

The `recall` command opens the object read-only. A read-only handle takes no writer lease and sees the committed manifest as it stood at open.

```python
namespace = cd.Namespace(os.environ["CHDB_DURABLE_URL"])
obj = namespace.open("acme-checkout-api", read_only=True)

result = obj.query(
    """
    SELECT id, content, kind, tags, source_ref
    FROM memories FINAL
    WHERE project_id = 'acme/checkout-api'
      AND status = 'active'
      AND (positionCaseInsensitiveUTF8(content, 'deploy') > 0
           OR has(tags, 'deployment'))
    ORDER BY pinned DESC, updated_at DESC
    LIMIT 5
    """,
    "JSONEachRow",
)
print(result.data())
obj.close()
```

The SQL runs locally. S3 is used during `open()`, `flush()`, and `checkpoint()`, not for every recall query.

The example ranks by typed fields and text so the storage lifecycle remains easy to follow. A memory system can fill the `embedding` column and use `cosineDistance` for semantic candidates without changing the durability model.

## Revising instead of overwriting history

The deployment region later changes to `eu-central-1`. The recipe inserts a newer `memories` row with the same ID and appends version 2 to `memory_history`.

`ReplacingMergeTree(updated_at)` and `FINAL` return the latest current row. The history table still answers how the decision changed. Forgetting follows the same pattern: insert a new current row with `status='contracted'` and append a `contract` history row.

The command finishes with `checkpoint()` because a completed revision pass is a useful compaction point:

```python
obj.execute("INSERT INTO memories ...")
obj.execute("INSERT INTO memory_history ...")
obj.execute("INSERT INTO events ...")
base_key = obj.checkpoint()
print(f"revision checkpoint: {base_key}")
```

## Practices that matter in production

### Flush when the application makes a promise

Batch low-value events if a short recovery window is acceptable. Flush a decision that changes future agent behavior before returning success. The interval between successful flushes is the amount of recent work the application has chosen to risk.

### Put deterministic statements in the WAL

Generate timestamps, UUIDs, random values, and model output in the application, then write literals. Avoid `now()`, `rand()`, `generateUUIDv4()`, and `INSERT ... SELECT` from a changing external source. Finish non-deterministic bulk work locally and use `checkpoint()` to preserve the resulting database.

### Checkpoint after work that shortens future recovery

Good checkpoint points include a bulk import, a large revision or conflict-resolution pass, the end of a long session, and a handoff to another host. Calling it after every row adds transfer work without improving the semantics of `flush()`.

### Let the object boundary follow the writer boundary

Durable V1 allows one writer per object and enforces that rule with a lease and fencing. One project or one user per object is usually a good fit. Workloads that require concurrent writers to one database should use separate objects or a server database.

The lease coordinates writers. Bucket IAM controls access. Give each tenant credentials scoped to its own prefix and use the storage provider's encryption controls where needed.

### Keep large binary values outside the database

Typed columns work well for project, kind, privacy, status, tags, and timestamps. Transcript text compresses well with ZSTD. Screenshots, audio, and other large binary values should live in object storage, with their keys recorded in a table. Otherwise each full checkpoint has to move the blobs again.

### Open one Durable object per process

chdb-core currently binds one data path per process. Scan a small number of objects one after another, or use worker processes for parallel fan-out.

## Other language bindings

Durable V1 defines the same object layout, WAL, checkpoints, lease behavior, fencing, and error categories for every binding. SQL and table definitions can stay the same.

Binding status, checked on 2026-09-15:

| Binding | Availability | Write-specific durability barrier |
|---|---|---|
| Python | Released in chDB 4.4.0 | Call `flush()` for the current buffer |
| Go | Tagged in `chdb-go` v2.2.0 | `FlushThrough(ctx, ticket)` |
| Node.js | Experimental on `main`; not in the v3.3.0 tag | `flushThrough(ticket)` |
| Rust | Experimental on `main`; not in the v1.4.0 tag | `flush_through(ticket)` |

| Operation | Python | Node.js | Go | Rust |
|---|---|---|---|---|
| Namespace | `cd.Namespace(...)` | `new DurableNamespace(...)` | `durable.NewNamespace(...)` | `Namespace::new(...)` |
| Open | `ns.open(id)` | `await ns.open(id)` | `ns.Open(ctx, id, opts)` | `ns.open(id, opts)` |
| Read | `obj.query(...)` | `await obj.query(...)` | `obj.Query(...)` | `obj.query(...)` |
| Write | `obj.execute(...)` | `await obj.execute(...)` | `obj.Execute(...)` | `obj.execute(...)` |
| Flush | `obj.flush()` | `await obj.flush()` | `obj.Flush(ctx)` | `obj.flush()` |
| Checkpoint | `obj.checkpoint()` | `await obj.checkpoint()` | `obj.Checkpoint(ctx)` | `obj.checkpoint()` |
| Close | `obj.close()` | `await obj.close()` | `obj.Close(ctx)` | `obj.close()` |

<details>
<summary>Node.js minimal writer</summary>

```ts
import { DurableNamespace, nodeEngineFactory } from 'chdb/durable/node'
import 'chdb/durable/s3'

const ns = new DurableNamespace('s3://my-agent-state/agent-memory', {
  engineFactory: nodeEngineFactory(),
  owner: 'node-agent',
})
const obj = await ns.open('acme-checkout-api', { database: 'mem' })
const ticket = await obj.execute(
  "INSERT INTO events VALUES ('evt-1', 'recall.run', 'node-agent', " +
  "'acme/checkout-api', '', 'recalled 1 memory', " +
  "'{\"hit_ids\":[\"mem-deploy-region\"]}', '2026-09-15 09:00:00.000')",
)
await obj.flushThrough(ticket)
await obj.close()
```

</details>

<details>
<summary>Go minimal writer</summary>

```go
ns, err := durable.NewNamespace(
    "s3://my-agent-state/agent-memory",
    durable.NamespaceOptions{Owner: "go-agent"},
)
if err != nil { log.Fatal(err) }

obj, _, err := ns.Open(ctx, "acme-checkout-api", durable.OpenOptions{Database: "mem"})
if err != nil { log.Fatal(err) }

ticket, err := obj.Execute(ctx,
    "INSERT INTO events VALUES ('evt-1', 'recall.run', 'go-agent', "+
        "'acme/checkout-api', '', 'recalled 1 memory', "+
        "'{\"hit_ids\":[\"mem-deploy-region\"]}', '2026-09-15 09:00:00.000')")
if err == nil { err = obj.FlushThrough(ctx, ticket) }
if closeErr := obj.Close(ctx); err == nil { err = closeErr }
if err != nil { log.Fatal(err) }
```

</details>

<details>
<summary>Rust minimal writer</summary>

```rust
use chdb_rust::durable::{Namespace, OpenOptions};

let ns = Namespace::new("s3://my-agent-state/agent-memory")?
    .with_owner("rust-agent");
let (obj, _) = ns.open(
    "acme-checkout-api",
    OpenOptions {
        database: Some("mem".to_string()),
        ..OpenOptions::default()
    },
)?;
let ticket = obj.execute(
    "INSERT INTO events VALUES ('evt-1', 'recall.run', 'rust-agent', \
     'acme/checkout-api', '', 'recalled 1 memory', \
     '{\"hit_ids\":[\"mem-deploy-region\"]}', '2026-09-15 09:00:00.000')",
)?;
obj.flush_through(ticket)?;
obj.close()?;
```

</details>

Check the binding's release notes before using a preview API in production.

## When to use a server

Use server-side ClickHouse, Postgres, or another shared service when several writers must update the same database, many clients need continuous online access, the working set is larger than local disk, or governance requires a centrally managed service.

Plain local chDB is enough when the database is disposable or stays on one host. A small transactional store is simpler when the application only saves runtime checkpoints and does not query memory history.

Durable fits workloads where one process owns a local analytical database at a time, queries should stay in-process, and committed state must be recoverable on another host.

## References

- [chDB Durable overview](https://github.com/chdb-io/chdb/blob/main/docs/durable/index.mdx)
- [Durable V1 contract](https://github.com/chdb-io/chdb/blob/main/dev-docs/CHDB_DURABLE_V1_CONTRACT.md)
- [Durable V1 protocol reference](https://github.com/chdb-io/chdb/blob/main/docs/durable/protocol-v1.mdx)
- [ClickMem schema at commit `7f9180c`](https://github.com/auxten/clickmem/blob/7f9180ce2a8f2d151bde43ba9eee2e9463afa573/src/clickmem/schema.py)
- [ClickMem recall implementation](https://github.com/auxten/clickmem/blob/7f9180ce2a8f2d151bde43ba9eee2e9463afa573/src/clickmem/recall.py)
- [Python v4.4.0 release](https://github.com/chdb-io/chdb/releases/tag/v4.4.0)
- [Go v2.2.0 Durable package](https://github.com/chdb-io/chdb-go/tree/v2.2.0/chdb/durable)
- [Node.js Durable source on `main`](https://github.com/chdb-io/chdb-node/tree/main/src/durable)
- [Rust Durable source on `main`](https://github.com/chdb-io/chdb-rust/tree/main/src/durable)
