// Scenario 1 — the core ingestion pipeline, end to end.
//
//   HTTP NDJSON stream ──(chunks, no JSON.parse)──▶ insert() ──▶ Null table otel_raw
//        │                                                        │ materialized view
//        │ peekField(first chunk): tenant for routing             │ (enrich in engine threads)
//        ▼                                                        ▼
//   service logic                                        MergeTree buffer `events`
//                                                                 │
//   model prices (JS bigint) ──object insert──▶ model_prices ─────┤ JOIN on `model`
//        decimal text, lossless                                   ▼
//                                  INSERT INTO FUNCTION remoteSecure(...) SELECT ...
//
// What this shows:
//   * the request body is streamed straight into the engine — JS never calls
//     JSON.parse and never assembles the body (no Buffer.concat); the C++
//     parser consumes the bytes chunk by chunk;
//   * one small field (tenant, for routing) is read with a byte-level scan of
//     the first streamed chunk — no object tree;
//   * model prices are born in JS as `bigint` (integer micro-USD) and reach
//     ClickHouse losslessly via decimal text — no 2^53 cliff;
//   * those prices never enter the hot byte stream: they live in a small side
//     table and are JOINed engine-side at export, on the `model` key.
//
// Run:  node scenario-1-ingest-join.mjs

import { createServer, request as httpRequest } from 'node:http'
import { once } from 'node:events'
import { Readable } from 'node:stream'
import chdb from 'chdb'
import { createSchema, peekField, spanLines } from './_shared.mjs'

const { Session } = chdb
const session = new Session() // a real service uses new Session('./data') for durability
createSchema(session)

// ---------------------------------------------------------------------------
// 1. Model prices as JS bigint. Money is integer micro-USD per 1M tokens; in JS
//    that means `bigint`. The object insert serializes each bigint to bare
//    decimal text, so the full 64-bit value lands in the UInt64 column exactly.
// ---------------------------------------------------------------------------
session.query(`
  CREATE TABLE model_prices (
    model            String,
    input_micro_usd  UInt64,   -- micro-USD per 1M input tokens
    output_micro_usd UInt64    -- micro-USD per 1M output tokens
  ) ENGINE = MergeTree ORDER BY model`)

await session.insert({
  table: 'model_prices',
  values: [
    { model: 'gpt-4o', input_micro_usd: 2_500_000n, output_micro_usd: 10_000_000n },
    { model: 'claude-fable-5', input_micro_usd: 3_000_000n, output_micro_usd: 15_000_000n },
  ],
})

// Prove the bigint survived byte-for-byte (no Number, no JSON.parse in the path).
const priceBack = BigInt(
  JSON.parse(session.query("SELECT toString(input_micro_usd) AS p FROM model_prices WHERE model='gpt-4o'", 'JSONEachRow')).p,
)
console.log('bigint round-trip :', priceBack === 2_500_000n ? 'exact (2500000n)' : `CORRUPT (${priceBack})`)

// ---------------------------------------------------------------------------
// 2. The ingestion endpoint: stream the request body straight into the engine.
//    A thin wrapper reads the tenant from the first chunk for routing, then
//    yields every chunk through unchanged — the body is never assembled in JS.
// ---------------------------------------------------------------------------
async function* withTenantPeek(stream, onTenant) {
  let scanned = false
  for await (const chunk of stream) {
    if (!scanned) { onTenant(peekField(chunk, 'tenant.id')); scanned = true }
    yield chunk
  }
}

const server = createServer(async (req, res) => {
  if (req.method !== 'POST' || req.url !== '/v1/spans') { res.writeHead(404); res.end(); return }
  let tenant = 'unknown'
  try {
    const summary = await session.insert({
      table: 'otel_raw',
      values: withTenantPeek(req, (t) => { tenant = t ?? tenant }),
      format: 'JSONEachRow',
      settings: { input_format_skip_unknown_fields: 1 },
    })
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ tenant, rowsSent: summary.rowsSent, chunks: summary.chunks }))
  } catch (e) {
    res.writeHead(e.failedAtRow !== undefined ? 400 : 500, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ error: e.code, reason: e.reason ?? null, failedAtRow: e.failedAtRow ?? null }))
  }
})
server.listen(0, '127.0.0.1')
await once(server, 'listening')
const base = `http://127.0.0.1:${server.address().port}`

// Client: stream 2000 spans as a chunked request body — produced lazily, never
// held as one batch.
const r = await new Promise((resolve, reject) => {
  const req = httpRequest(base + '/v1/spans', { method: 'POST' }, (res) => {
    let out = ''
    res.on('data', (d) => { out += d })
    res.on('end', () => resolve(JSON.parse(out)))
  })
  req.on('error', reject)
  Readable.from(spanLines(0, 2000, 'acme')).pipe(req)
})
console.log('ingest            :', JSON.stringify(r), '(streamed chunked body, never JSON.parsed or assembled in JS)')

// ---------------------------------------------------------------------------
// 3. Enrichment ran in the engine; the buffer is a real ClickHouse table.
// ---------------------------------------------------------------------------
console.log('ns precision      :',
  session.query("SELECT toString(start_ns) FROM events WHERE span_id='span-000000'", 'CSV').trim())

// ---------------------------------------------------------------------------
// 4. Export = the merge point. The JOIN with model_prices happens engine-side;
//    cost is computed from the bigint prices and the token counts the view
//    extracted. The high-traffic span stream never carried a price, and nothing
//    surfaces back into JS — the engine speaks the native protocol from its own
//    threads (or, with no server configured, copies to a local stand-in table).
// ---------------------------------------------------------------------------
const exportSQL = (target) => `
  INSERT INTO ${target}
  SELECT
    e.trace_id, e.span_id, e.model, e.tenant,
    e.input_tokens, e.output_tokens,
    -- cost in micro-USD: tokens * price_per_1M / 1_000_000, integer-exact
    e.input_tokens  * p.input_micro_usd  / 1000000
      + e.output_tokens * p.output_micro_usd / 1000000           AS cost_micro_usd
  FROM events e
  LEFT JOIN model_prices p USING (model)`

if (process.env.CLICKHOUSE_HOST) {
  const target = `FUNCTION remoteSecure('${process.env.CLICKHOUSE_HOST}:9440', 'otel.events_priced', '${process.env.CLICKHOUSE_USER ?? 'default'}', '${process.env.CLICKHOUSE_PASSWORD}')`
  await session.queryAsync(exportSQL(target))
  console.log('export            : pushed priced rows to', process.env.CLICKHOUSE_HOST, 'over the native protocol')
} else {
  session.query(`
    CREATE TABLE events_priced (
      trace_id String, span_id String, model String, tenant String,
      input_tokens UInt64, output_tokens UInt64, cost_micro_usd UInt64
    ) ENGINE = MergeTree ORDER BY (model, trace_id)`)
  await session.queryAsync(exportSQL('events_priced'))
  console.log('\npriced sample (engine-side JOIN, bigint prices):')
  console.log(session.query(`
    SELECT model, count() AS spans, sum(cost_micro_usd) AS total_micro_usd
    FROM events_priced GROUP BY model ORDER BY model`, 'Pretty'))
}

server.close()
session.close()
