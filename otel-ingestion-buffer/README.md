# OTEL ingestion buffer in Node.js — S3 → ClickHouse with chDB

Move OTEL/LLM-trace data from S3 into ClickHouse from a Node.js service **without
the rows ever passing through JavaScript** — the Langfuse-style "data is already
on S3" workload. The payload sits on S3 as **length-delimited protobuf**, keyed by
entity (`s3://…/{projectId}/observation/{eventBodyId}/*.pb`). Per entity, Node
hands chDB a glob path and nothing else; **one SQL statement** reads + parses the
protobuf, field-merges the create/update partial events, enriches (prompt/model/
price lookups + token counting), and exports to a ClickHouse server over the
native protocol. JavaScript never calls `JSON.parse`, never merges rows, never
touches a socket.

> Design of record: [`../langfuse_chdb_final_design_en.md`](../langfuse_chdb_final_design_en.md). Everything here follows it.

> **Requires** `chdb@3.1.0-rc.2` (chdb-node on the chdb-core v26.5.1-rc.1 engine), Node 20+, and `protobufjs` (to generate the sample `.pb`). The scenarios run end to end against embedded chDB.
>
> ```bash
> npm install
> ```

## Why pull-from-S3, and why no `JSON.parse`

A Node service that moves large JSON through the JS data plane hits four walls:

1. **Event-loop freeze** — `JSON.parse`/`JSON.stringify` are synchronous and uninterruptible; a big batch freezes the loop exactly when the service is busiest.
2. **String ceiling** — V8 caps a single string at ~512 MB; accumulating batches into one string eventually throws `RangeError`.
3. **Integer precision** — JS `number` loses precision past 2^53, so nanosecond timestamps (~1.8 × 10^18) silently corrupt unless carried as strings.
4. **Socket backpressure** — writing to upstream sockets from JS means manual buffering, backpressure bookkeeping, and GC pressure, which feeds back into wall 1.

Same disease, four symptoms: the data has to become V8-heap objects/strings on the main thread, and the socket has to be driven from the event loop. chDB removes the whole data plane from JS — the engine reads S3, parses, merges, enriches, and ships from its own C++ threads — so all four stop existing rather than being mitigated.

```
  S3 (length-delimited protobuf, by entity)
       │   per entity, JS passes only a glob path  's3://…/{eventBodyId}/*.pb'
       ▼
  chDB (embedded, in the Node process) — ONE statement:
       s3('…/{eventBodyId}/*.pb','Protobuf')           read + parse protobuf       [engine, multi-thread]
        │  GROUP BY id, argMaxIf(col, ts, col<>'')      field-merge create+update   [engine]
        ▼
       dictGet(prompt) + match(model) + dictGet(price)  enrich (lookups ← Postgres) [engine]
        │  bpe_count(text, tokenizer)                   token count                 [engine; WASM UDF]
        ▼
       INSERT INTO FUNCTION remoteSecure(...)           export                      [engine, native protocol]
       ▼
  ClickHouse Cloud — ReplacingMergeTree / AggregatingMergeTree (cross-chunk merge backstop)
```

The field-merge (`argMaxIf … GROUP BY id`) collapses the create event (which carries `input`/`model`/`prompt`) and the later update event (which carries only `output`) into one full row — **replacing the app-side read-modify-write** entirely. Nanosecond integers stay in `UInt64` exactly; nothing ever becomes a V8 number or string.

## Scenarios

Three runnable demos. The shared scaffolding — the protobuf generator, the
reference dictionaries + token UDF, and the one canonical per-entity SQL — lives
in [`_shared.mjs`](./_shared.mjs); the schema is [`observation.proto`](./observation.proto).

```bash
npm install
npm run scenario:1   # the core pipeline (S3 protobuf → merge → enrich → export)
npm run scenario:2   # flow & resource control
npm run scenario:3   # failures & retries
```

- **[Scenario 1 — the core pipeline](./scenario-1-ingest-join.mjs)** — end to end: per entity, one chDB statement globs the entity's `.pb` files, field-merges the create/update partials with `argMaxIf … GROUP BY id`, enriches via `dictGet` (prompt/price) + `match()` (model) + the token UDF, and exports. Verifies the merge kept the create event's `input` even when the latest event carried only `output` — engine-side, no read-modify-write in JS.
- **[Scenario 2 — flow & resource control](./scenario-2-flow-control.mjs)** — there is no HTTP body to backpressure, so flow control is three things: **(A)** JS heap stays flat ingesting a 2 MB entity (data is off the V8 heap); **(B)** a single job's footprint is bounded by engine `SETTINGS` (`max_memory_usage`, `max_threads`), and a too-low cap fails with a typed `MEMORY_LIMIT_EXCEEDED` you handle; **(C)** one session serializes queries (per-process singleton, each internally multi-threaded) → you scale by adding Fargate tasks, not threads.
- **[Scenario 3 — failures & retries](./scenario-3-error-retry.mjs)** — the unit of work is one per-entity statement: **(1)** a failed export → just re-run it; the destination `ReplacingMergeTree` dedups by `(id, event_ts)` and the merge is order-independent, so re-runs and concurrent writers converge with no lock; **(2)** a corrupt `.pb` → a typed parse error isolated to that entity, other jobs unaffected; **(3)** a late-arriving update → re-processing emits a higher-`event_ts` merged row and the destination keeps the latest.

## What's real vs. pending

Runs today on `chdb@3.1.0-rc.2`, with these substitutions for a self-contained demo (production form is in the comments at each call site in `_shared.mjs`):

| Demo (runs now) | Production |
| --- | --- |
| `file('…/{eventBodyId}/*.pb','Protobuf')` over local files | `s3('s3://…/{eventBodyId}/*.pb','Protobuf')` — same glob, same format |
| `bpe_count` SQL UDF (`length/4`) | `CREATE FUNCTION bpe_count LANGUAGE WASM …` (tiktoken-rs → wasm, vocab embedded). Needs chdb-core to enable the upstream WASM-UDF runtime (today: `SUPPORT_IS_DISABLED`). |
| dictionaries `SOURCE(CLICKHOUSE(TABLE …))` | `SOURCE(POSTGRESQL(…))` — `complex_key_cache` (read-through) for prompts, `hashed`+`LIFETIME` for model/price |
| `INSERT INTO events_priced` (local) | `INSERT INTO FUNCTION remoteSecure(…)` — native protocol |

## Production notes

- **Framing**: length-delimited protobuf — each message prefixed by a varint byte-length (`protobufjs` `encodeDelimited` = ClickHouse's `Protobuf` format). Pass the schema with `SETTINGS format_schema='observation.proto:Observation'` (the `.proto` can also be an inline string via `format_schema_source='string'`).
- **The glob reads everything**: `s3('…/*.pb')` follows the S3 `ListObjectsV2` continuation token across all pages; `s3_list_object_keys_size` is only the per-page size, not a cap. `*` matches within one path segment, so `{eventBodyId}/*.pb` is exactly that entity's files. (Completeness is bounded only by S3 LIST consistency — strongly consistent on AWS since 2020-12; verify for MinIO/compatible stores.)
- **Dictionaries replace Redis on this path**: a chDB dictionary *is* an in-engine cache over Postgres (the source of truth), so source the dicts straight from PG. model/price = `hashed`+`LIFETIME`; prompt = `complex_key_cache` (read-through, so a brand-new prompt version resolves on a miss). The dictionary lives at the worker **process** scope and stays warm across jobs (the worker is a long-running consumer; jobs are short, the process is not).
- **Cross-chunk / cross-time merge is the destination's job**: if the producer writes full rows, use `ReplacingMergeTree(event_ts)`; if it writes partial deltas, use `AggregatingMergeTree` + per-column `argMaxIf(col, event_ts, col<>'')`. Both are order-independent, so multiple Fargate writers need no lock — confirm with your producer which one applies.
- **Scale-out is process-level**: one chDB session per process (a hard singleton — different data path → `Code:36`), each saturating its task's vCPUs on a single statement. Add Fargate tasks to scale throughput; don't open worker_threads for parallelism.
- **Big chunks are orthogonal**: writing fewer, larger `.pb` files (MB–100MB) kills the S3 small-file request volume, but the design works unchanged on per-entity files (this demo).

## Interface — the per-entity statement

The whole pipeline is one statement, built by `entityInsertSQL(glob)` in `_shared.mjs`. Production swaps `file()`→`s3()` and the target table → `INSERT INTO FUNCTION remoteSecure(...)`; nothing else changes.

```sql
INSERT INTO <target>
WITH merged AS (
  SELECT
    id,
    argMaxIf(input,          ts, input          != '') AS input,    -- field-level merge:
    argMaxIf(output,         ts, output         != '') AS output,   --  create + update partials
    argMaxIf(model,          ts, model          != '') AS model,    --  collapse to one full row
    argMaxIf(prompt_name,    ts, prompt_name    != '') AS prompt_name,
    argMaxIf(prompt_version, ts, prompt_version != 0)  AS prompt_version,
    max(ts) AS event_ts
  FROM s3('s3://…/{eventBodyId}/*.pb','Protobuf')                   -- read + parse protobuf
  GROUP BY id
)
SELECT
  m.id, m.model, m.event_ts,
  dictGet('prompt_dict','id',(m.project_id, m.prompt_name, m.prompt_version)) AS prompt_id,
  md.model_id,
  bpe_count(m.input,  md.tokenizer)  AS input_tokens,              -- WASM UDF in production
  bpe_count(m.output, md.tokenizer)  AS output_tokens,
  input_tokens * dictGet('price_dict','in_micro', tuple(md.model_id)) / 1e6 AS total_cost
FROM merged AS m, models AS md
WHERE match(m.model, md.pattern)                                   -- model regex match
SETTINGS format_schema = 'observation.proto:Observation'
```

From Node, this is just:

```js
import chdb from 'chdb'
import { entityGlob, entityInsertSQL, setupEngine } from './_shared.mjs'

const session = new chdb.Session()
setupEngine(session)                                   // dicts + token UDF + destination (once)
session.query(entityInsertSQL(entityGlob(DATA, eventBodyId)))   // per job: pass a path, await
```

## Try next

- **Enable the WASM UDF** in chdb-core (the upstream runtime is merged but `SUPPORT_IS_DISABLED`), then swap the `bpe_count` placeholder for a tiktoken-rs WASM module.
- **Point the dictionaries at Postgres** (`SOURCE(POSTGRESQL(…))`) and the export at `remoteSecure(...)` to run against a real Langfuse stack.
- **Switch the destination to `AggregatingMergeTree` + argMaxIf** if your S3 events are partial deltas rather than coalesced full rows.
