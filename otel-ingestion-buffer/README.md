# OTEL ingestion buffer in Node.js

Use chDB as an off-heap ingestion buffer inside a Node.js service that receives
OTEL/LLM-trace JSON — the Langfuse-style workload: wide span attributes carrying
full prompts and completions, nanosecond timestamps. The body bytes are handed
to the engine without ever calling `JSON.parse` on the main thread, rows are
enriched inside the engine with a materialized view, and the buffer is exported
to a ClickHouse server over the native protocol — without the data ever
surfacing back into JavaScript.

> **Requires** the raw/streaming insert API — `insert({ values: Buffer | Readable, format })` — which ships in the **`chdb@3.1.0-rc.2`** prerelease (chdb-node on the chdb-core v26.5.1-rc.1 engine). Node 20+.
>
> ```bash
> npm install chdb@3.1.0-rc.2   # or: npm install chdb@next
> ```
>
> The stable `chdb@3.0.0` (chdb-core v26.5.0) does **not** include this API — install the prerelease above to run these scenarios.

## Why a buffer, and why not `JSON.parse`

A Node ingestion service that handles large JSON the usual way hits four walls:

1. `JSON.parse`/`JSON.stringify` are synchronous and uninterruptible — a big batch freezes the event loop exactly when the service is busiest.
2. V8 caps a single string at ~512 MB — accumulating batches into strings eventually throws `RangeError`.
3. JS `number` loses precision past 2^53 — nanosecond timestamps (~1.8 × 10^18) silently corrupt.
4. Writing to upstream sockets from JS means buffering, backpressure bookkeeping, and GC pressure.

chDB removes all four by keeping the payload a `Buffer` (off the V8 heap) and
letting the engine — a multithreaded C++ parser — do every byte of parsing, then
having the engine push to the server from its own threads. JavaScript's job is to
pass a reference. chDB here is closest to an **embedded OTel-collector / Vector
buffer whose buffer-and-transform stage is a full ClickHouse SQL engine**: it
receives, absorbs bursts, transforms in SQL, and ships over the native protocol —
the socket never lands on your event loop.

Two lanes feed the buffer; they merge engine-side at export:

```
Lane 1 — hot path (spans):
  HTTP NDJSON body ──(Buffer, zero-copy)──▶ insert() ──▶ otel_raw (Null) ──MV──▶ events (MergeTree)
       └ peekField(first chunk) → tenant routing   (byte-level SAX scan, no JSON.parse)

Lane 2 — reference data (prices):
  model prices (JS bigint, decimal text → lossless UInt64) ──object insert──▶ model_prices

Merge & ship (engine-side, at export):
  events  JOIN model_prices ON `model`  ──▶  INSERT INTO FUNCTION remoteSecure(...) SELECT  ──▶  remote ClickHouse
```

The hot lane never assembles the body or calls `JSON.parse`; it streams the bytes
straight in and, where the service needs one small field (tenant routing, a
sampling key), reads it with a byte-level scan of the first chunk — no object
tree. The reference lane keeps prices out of the high-traffic stream: they live in
a small table and are JOINed on the `model` key at export. Nanosecond timestamps
ride as JSON strings and land in `UInt64` exactly; prices are born as `bigint` and
serialize to decimal text, so neither hits the 2^53 cliff.

## Scenarios

Three runnable demos, each self-contained. The shared schema and helpers (the
`Null` + materialized-view funnel, the byte-level field peek, a synthetic span
generator) live in [`_shared.mjs`](./_shared.mjs).

```bash
npm install          # links chdb
npm run scenario:1   # the core pipeline
npm run scenario:2   # flow control
npm run scenario:3   # failures & retries
```

- **[Scenario 1 — the core pipeline](./scenario-1-ingest-join.mjs)** — the shape above, end to end: streamed NDJSON ingest with a byte-level tenant peek, plus `bigint` model-price reference data joined engine-side at export. Proves the `bigint` round-trips losslessly (decimal text → `UInt64`) and that prices never enter the hot byte stream.
- **[Scenario 2 — flow control](./scenario-2-flow-control.mjs)** — bounded memory (O(chunk), not O(body)) and end-to-end backpressure, plus the three typed flow-control faults and how to handle each: `stall` → release the connection / 408, `backpressure-overflow` → fix the producer to honor pause or raise `maxBufferedBytes`, `row-too-large` → reject the client or raise `maxRowBytes`.
- **[Scenario 3 — failures & retries](./scenario-3-error-retry.mjs)** — export side: the buffer is the safety net, so a failed export loses nothing and retry is idempotent via a `ReplacingMergeTree` key. Ingest side: errors are typed, a failed chunk lands zero rows, and streamed inserts are at-least-once → resend (dedup downstream).

## Production notes

- Use a persistent session path (`new Session('./otel-buffer-data')`) so the buffer survives restarts. One chDB session per process; route all inserts through it.
- The streaming lane only accepts line-delimited formats where a raw `\n` is guaranteed to be a row boundary — `JSONEachRow`, `JSONCompactEachRow`, `TabSeparated` (ClickHouse escapes newlines inside values). `CSV` and `*WithNames` are rejected for streams. Raw OTLP (protobuf, or the single-JSON-object OTLP/HTTP body) is **not** NDJSON: put a collector/shipper in front to emit one compact JSON object per line.
- `input_format_skip_unknown_fields: 1` keeps wide OTEL payloads insertable while your schema models only what you query. Other `input_format_*` settings pass through the same `settings` option.
- Nanosecond timestamps must travel as JSON **strings** (`"start_ns":"1780000000000000001"`) — the engine parses them into `UInt64` exactly, as scenario 1 verifies.
- The `rowsSent` vs `rowsWritten` summary counts differ on purpose: `rowsSent` is the payload lines handed over; `rowsWritten` is the engine's write count, which includes rows a materialized view wrote (with one view attached, expect `rowsWritten ≈ 2 × rowsSent`) — the same semantics as ClickHouse's HTTP `X-ClickHouse-Summary`.
- `JSON.stringify` throws on `BigInt` — when object-born rows carry 64-bit values, serialize with a replacer: `JSON.stringify(row, (k, v) => typeof v === 'bigint' ? String(v) : v)`. The object `insert()` path already handles `bigint` directly (scenario 1).
- Prices are joined at export against the *current* `model_prices`. For point-in-time cost (the price as of the event), make `model_prices` a `dictionary` and call `dictGet(...)` inside the materialized view so the price is baked into each row at write time. (The JOIN, the dictionary, and `dictGet`-in-MV were all verified on embedded chDB v26.5.1.)
- If your data is born as JS objects (per-row enrichment in JS), don't `stringify` the whole batch on the main thread in one go: serialize in a `worker_thread` and transfer the `ArrayBuffer` back for a raw insert, or yield NDJSON strings from an async generator into the streaming insert. Both keep stalls bounded; neither is free.

## Interface — streaming `insert()`

Use this path when your high-traffic payload is already serialized as NDJSON, for example OTEL traces delivered through an HTTP request body.

The important part is: do not `JSON.parse()` or `JSON.stringify()` the whole payload on the main thread. Pass the byte stream directly to `insert()` with `format: 'JSONEachRow'`. chdb-node will consume the stream with backpressure, cut chunks only at row boundaries, and write each chunk through the native raw insert path.

```js
import http from 'node:http'
import chdb from 'chdb'

const { Session } = chdb
const session = new Session()

session.query(`
  CREATE TABLE IF NOT EXISTS otel_traces
  (
    trace_id String,
    span_id String,
    parent_span_id String,
    name String,
    start_ns UInt64,
    end_ns UInt64,
    attributes_json String
  )
  ENGINE = MergeTree
  ORDER BY (trace_id, span_id)
`)

const server = http.createServer(async (req, res) => {
  if (req.method !== 'POST' || req.url !== '/ingest/traces') {
    res.writeHead(404).end()
    return
  }

  try {
    const summary = await session.insert({
      table: 'otel_traces',
      values: req,
      format: 'JSONEachRow',

      // Optional: only insert the columns modeled in the table.
      // Unknown fields in a wider OTEL payload are skipped by the engine.
      settings: {
        input_format_skip_unknown_fields: 1,
      },

      // Optional streaming controls.
      maxChunkBytes: 8 * 1024 * 1024,
      maxRowBytes: 64 * 1024 * 1024,
      maxBufferedBytes: 64 * 1024 * 1024,
      stallTimeout: 30_000,

      onProgress: (p) => {
        console.log('insert progress', p)
      },
    })

    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({
      ok: true,
      rowsSent: summary.rowsSent,
      rowsWritten: summary.rowsWritten,
      bytesSent: summary.bytesSent,
      bytesWritten: summary.bytesWritten,
      chunks: summary.chunks,
      elapsed: summary.elapsed,
    }))
  } catch (e) {
    res.writeHead(500, { 'content-type': 'application/json' })
    res.end(JSON.stringify({
      ok: false,
      code: e.code,
      reason: e.reason,
      failedAtRow: e.failedAtRow,
      progress: e.progress,
      message: e.message,
    }))
  }
})

server.listen(3000)
```

### Input

`values: req` passes the HTTP request body directly as the streaming payload. The stream must yield bytes: `Buffer`, `Uint8Array`, or `string`.

`format: 'JSONEachRow'` tells chDB to parse the payload as NDJSON: one JSON object per line. Only line-delimited formats are accepted for streams (`JSONEachRow`, `JSONCompactEachRow`, `TabSeparated`); `CSV` and `*WithNames` are rejected.

`settings.input_format_skip_unknown_fields: 1` is useful for wide OTEL payloads. Your table can model only the fields you query, while chDB skips extra JSON fields during insert.

### Streaming controls

`maxChunkBytes` is the target chunk size. Default: `8 MiB`.

chdb-node accumulates incoming bytes until it reaches roughly this size, then cuts only at the latest `\n` row boundary and inserts that chunk.

`maxRowBytes` is the largest allowed single row. Default: `64 MiB`.

If no newline is seen and the buffered row grows past this limit, insert fails with `reason: 'row-too-large'`.

`maxBufferedBytes` protects against sources that ignore backpressure. Default: `64 MiB`.

If a push-style `Readable` has already buffered more than this, insert fails with `reason: 'backpressure-overflow'` instead of buffering unboundedly.

`stallTimeout` is an optional producer idle timeout.

If the source neither yields data nor ends within this time, insert fails with `ChdbTimeoutError` and `reason: 'stall'`.

`onProgress` is called after each chunk is successfully written.

```js
onProgress: (p) => {
  // p = { rowsSent, bytesSent, chunks }
}
```

### Output

A successful streaming insert returns:

```js
{
  rowsWritten: 100000,
  bytesWritten: 12345678,
  rowsSent: 100000,
  bytesSent: 12345678,
  chunks: 12,
  elapsed: 1.23
}
```

`rowsSent` is the payload-side count: non-empty NDJSON lines successfully flushed.

`rowsWritten` is the engine-side count returned by chDB / ClickHouse. It may include materialized-view cascade writes, so it can be larger than `rowsSent`.

`bytesSent` is the number of payload bytes flushed from the stream.

`bytesWritten` is the engine-side written-byte counter.

`chunks` is how many bounded chunks were inserted.

`elapsed` is the accumulated engine execution time across chunks.

### Error handling

Streaming insert is at-least-once. Chunks already written before an error are not rolled back.

For retry, resend from your source and deduplicate downstream if needed. Treat `failedAtRow` and `progress` as observability fields, not as a resume cursor.

Common errors:

`reason: 'source-error'`

The producer stream failed or closed unexpectedly.

`reason: 'write-failure'`

chDB rejected one chunk, for example because of malformed JSON or a type mismatch. The error may include `failedAtRow`.

`reason: 'row-too-large'`

A single NDJSON row exceeded `maxRowBytes`.

`reason: 'backpressure-overflow'`

The source ignored backpressure and buffered beyond `maxBufferedBytes`.

`reason: 'stall'`

The producer stopped yielding data and did not end before `stallTimeout`.

`code: 'CHDB_ABORT'`

The insert was aborted through `AbortSignal`.

## Try next

- **Worker-thread serialization bridge** — for object-born rows: stringify off the main thread, transfer the bytes, insert raw.
- **Checkpointed export** — resumable, part-based export with O(1) buffer cleanup (recipe in progress).
