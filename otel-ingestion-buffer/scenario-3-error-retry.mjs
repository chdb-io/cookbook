// Scenario 3 — failures and retries. Two surfaces fail, and they retry
// differently; the buffer is what makes both safe.
//
//   A. Export side (ClickHouse unreachable): rows stay in the local buffer, so
//      retry = run the export again. A ReplacingMergeTree keyed on
//      (trace_id, span_id) makes a re-run idempotent.
//   B. Ingest side (bad payload / producer dies): errors are typed and a failed
//      chunk lands zero rows, so retry = resend. Streaming inserts are
//      at-least-once — failedAtRow/progress are observability, not a cursor.
//
// Run:  node scenario-3-error-retry.mjs

import { Readable } from 'node:stream'
import chdb from 'chdb'
import { createSchema, span, spanLines } from './_shared.mjs'

const { Session } = chdb
const session = new Session()
createSchema(session)

const count = () => Number(session.query('SELECT count() FROM events', 'CSV').trim())

// Seed the buffer with good data, streamed in.
await session.insert({ table: 'otel_raw', values: Readable.from(spanLines(0, 1000, 'acme')),
  format: 'JSONEachRow', settings: { input_format_skip_unknown_fields: 1 } })
console.log('buffered           :', count(), 'rows ready to export\n')

// ===========================================================================
// A. Export when ClickHouse is down → data stays in the buffer → retry.
// ===========================================================================
const exportTo = (target) => session.queryAsync(`
  INSERT INTO ${target}
  SELECT trace_id, span_id, start_ns, duration_ms, name, model,
         input_tokens, output_tokens, prompt_preview, tenant, received_at
  FROM events`, { timeout: 4000 })

// First attempt: server unreachable (a closed port stands in for "CH is down").
let exported = false
try {
  await exportTo(`FUNCTION remoteSecure('127.0.0.1:1', 'otel.events', 'default', 'x')`)
  exported = true
} catch (e) {
  console.log('A. export attempt  : FAILED —', e.code, '(ClickHouse unreachable)')
  console.log('   buffer intact   :', count(), 'rows still in the buffer — nothing lost')
}

// Retry: ClickHouse is back. Here a local ReplacingMergeTree stands in for the
// server table; the dedup key makes a re-run safe even if a prior export had
// half-succeeded.
session.query(`
  CREATE TABLE warehouse (
    trace_id String, span_id String, start_ns UInt64, duration_ms Float64,
    name String, model String, input_tokens UInt64, output_tokens UInt64,
    prompt_preview String, tenant String, received_at DateTime64(9)
  ) ENGINE = ReplacingMergeTree ORDER BY (trace_id, span_id)`)
if (!exported) {
  await exportTo('warehouse')
  await exportTo('warehouse')                       // retry again — idempotent by construction
  session.query('OPTIMIZE TABLE warehouse FINAL')   // force the dedup merge for the demo
  const dst = Number(session.query('SELECT count() FROM warehouse', 'CSV').trim())
  console.log('   retry succeeded :', dst, 'rows exported (two runs collapsed to one by ReplacingMergeTree)\n')
}

// ===========================================================================
// B1. A corrupt line: the chunk is rejected, zero rows land.
// ===========================================================================
const before = count()
const corruptLines = [span(9001, 'acme') + '\n', '{this is not json\n', span(9002, 'acme') + '\n']
try {
  await session.insert({ table: 'otel_raw', values: Readable.from(corruptLines),
    format: 'JSONEachRow', settings: { input_format_skip_unknown_fields: 1 } })
} catch (e) {
  console.log('B1. corrupt batch  : rejected —', e.code, 'reason=' + e.reason, 'failedAtRow=' + e.failedAtRow,
    '| rows landed:', count() - before, '(failed chunk lands zero rows)')
}
// Fix the offending line and resend: it succeeds.
await session.insert({ table: 'otel_raw',
  values: Readable.from([span(9001, 'acme') + '\n', span(9003, 'acme') + '\n', span(9002, 'acme') + '\n']),
  format: 'JSONEachRow', settings: { input_format_skip_unknown_fields: 1 } })
console.log('    resend fixed   : +' + (count() - before), 'rows landed after fixing the line\n')

// ===========================================================================
// B2. The producer dies mid-stream → the insert settles with reason
//     'source-error' — no hang, no unhandledRejection.
// ===========================================================================
const broken = new Readable({ read() {} })
broken.push(span(0, 'acme') + '\n')
broken.push(span(1, 'acme') + '\n')
queueMicrotask(() => broken.destroy(new Error('client connection reset')))
try {
  await session.insert({ table: 'otel_raw', values: broken, format: 'JSONEachRow',
    settings: { input_format_skip_unknown_fields: 1 }, maxChunkBytes: 1 * 1024 * 1024 })
} catch (e) {
  console.log('B2. producer died  : settled —', JSON.stringify({ code: e.code, reason: e.reason }),
    '(at-least-once: flushed chunks stay, retry = resend from your source)')
}

session.close()
