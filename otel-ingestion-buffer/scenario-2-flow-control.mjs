// Scenario 2 — flow control: keeping memory bounded, and turning every
// backpressure-adjacent failure into a typed error you can act on.
//
// The streaming insert consumes its source pull-based: at most ~maxChunkBytes
// is resident, and while a chunk's INSERT is in flight the source is paused, so
// a fast producer is throttled to the engine's write rate. Backpressure itself
// is never an error; the four failures below are, and each carries an
// `e.progress` snapshot:
//
//   maxChunkBytes      target chunk size          — bounds resident memory
//   stallTimeout       producer idle deadline     — reason 'stall'
//   maxBufferedBytes   ceiling for push sources   — reason 'backpressure-overflow'
//   maxRowBytes        single-row ceiling         — reason 'row-too-large'
//
// Run:  node scenario-2-flow-control.mjs

import { createServer, request as httpRequest } from 'node:http'
import { once } from 'node:events'
import { Readable } from 'node:stream'
import chdb from 'chdb'
import { createSchema, span } from './_shared.mjs'

const { Session } = chdb
const session = new Session()
createSchema(session)

// ---------------------------------------------------------------------------
// (a) bounded memory + (b) end-to-end backpressure. The endpoint streams the
//     request body in; maxChunkBytes caps what is resident, and pausing the
//     source during each insert propagates back through the socket to the
//     client's TCP window — the client slows down instead of the heap growing.
// ---------------------------------------------------------------------------
let lastProgress = null
const server = createServer(async (req, res) => {
  if (req.url !== '/stream') { res.writeHead(404); res.end(); return }
  try {
    const summary = await session.insert({
      table: 'otel_raw',
      values: req,                       // the request stream itself — not assembled in memory
      format: 'JSONEachRow',
      settings: { input_format_skip_unknown_fields: 1 },
      maxChunkBytes: 1 * 1024 * 1024,    // at most ~1 MiB resident at a time
      stallTimeout: 30_000,
      onProgress: (p) => { lastProgress = p },
    })
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ rowsSent: summary.rowsSent, chunks: summary.chunks }))
  } catch (e) {
    res.writeHead(e.reason === 'stall' ? 408 : 500, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ error: e.code, reason: e.reason ?? null }))
  }
})
server.listen(0, '127.0.0.1')
await once(server, 'listening')
const base = `http://127.0.0.1:${server.address().port}`

// Push 40k spans (~75 MiB) through one streamed insert, counting how often the
// client is told to slow down. The body is never assembled — it flows in chunks.
let writeFalseCount = 0
const result = await new Promise((resolve, reject) => {
  const req = httpRequest(base + '/stream', { method: 'POST' }, (res) => {
    let out = ''
    res.on('data', (d) => { out += d })
    res.on('end', () => resolve(JSON.parse(out)))
  })
  req.on('error', reject)
  let i = 0
  const writeSome = () => {
    while (i < 40_000) {
      const ok = req.write(span(i++, 'acme') + '\n')
      if (!ok) {                         // kernel write buffer full → engine is the bottleneck
        writeFalseCount++
        req.once('drain', writeSome)
        return
      }
    }
    req.end()
  }
  writeSome()
})

console.log('(a) bounded memory : streamed', result.rowsSent, 'spans as', result.chunks,
  'chunks of ≤1 MiB — resident memory is O(chunk), not O(body ~75 MiB)')
console.log('    progress       :', JSON.stringify(lastProgress))
console.log('(b) backpressure   : client write() returned false', writeFalseCount,
  'times → the producer was paused via TCP, not buffered on the heap')

// ---------------------------------------------------------------------------
// (c) Stuck producer → reason 'stall'. A finite stream that goes silent would
//     otherwise hold a connection and a chunk buffer open forever. stallTimeout
//     fails the insert fast. Handling: release the connection, answer 408.
// ---------------------------------------------------------------------------
const stalling = new Readable({ read() {} })
stalling.push(span(0, 'acme') + '\n')    // one line, then silence: no more data, no end()
try {
  await session.insert({
    table: 'otel_raw', values: stalling, format: 'JSONEachRow',
    settings: { input_format_skip_unknown_fields: 1 },
    maxChunkBytes: 1 * 1024 * 1024,
    stallTimeout: 500,
  })
} catch (e) {
  console.log('(c) stall          :', JSON.stringify({ reason: e.reason, progress: e.progress }),
    '→ release the connection, respond 408')
}
stalling.destroy()

// ---------------------------------------------------------------------------
// (d) Push-style source that ignores backpressure → reason 'backpressure-overflow'.
//     Bytes are shoved into a Readable faster than it drains, so readableLength
//     runs past maxBufferedBytes; the insert refuses to buffer unboundedly.
//     Handling: the producer is the bug — make it pull-based / honor pause, or
//     raise maxBufferedBytes for a known-bounded burst.
// ---------------------------------------------------------------------------
const flooding = new Readable({ read() {} })
for (let i = 0; i < 4096; i++) flooding.push(span(i, 'acme') + '\n')   // ~8 MiB shoved in up front
flooding.push(null)
try {
  await session.insert({
    table: 'otel_raw', values: flooding, format: 'JSONEachRow',
    settings: { input_format_skip_unknown_fields: 1 },
    maxChunkBytes: 256 * 1024,
    maxBufferedBytes: 1 * 1024 * 1024,   // refuse to hold > 1 MiB from a source that won't pause
  })
} catch (e) {
  console.log('(d) overflow       :', JSON.stringify({ reason: e.reason, progress: e.progress }),
    '→ fix the producer to honor pause, or raise maxBufferedBytes')
}
flooding.destroy()

// ---------------------------------------------------------------------------
// (e) A single row with no boundary → reason 'row-too-large'. One ~512 KiB run
//     of bytes with no '\n' grows the accumulator past maxRowBytes with nowhere
//     to cut. Handling: reject the client (payload is malformed / not
//     line-delimited), or raise maxRowBytes if such rows are expected.
// ---------------------------------------------------------------------------
const giant = new Readable({ read() {} })
giant.push(Buffer.alloc(512 * 1024, 0x41))   // 512 KiB of 'A', no newline
giant.push(null)
try {
  await session.insert({
    table: 'otel_raw', values: giant, format: 'JSONEachRow',
    settings: { input_format_skip_unknown_fields: 1 },
    maxChunkBytes: 64 * 1024,
    maxRowBytes: 256 * 1024,
  })
} catch (e) {
  console.log('(e) row too large  :', JSON.stringify({ reason: e.reason, progress: e.progress }),
    '→ reject the client, or raise maxRowBytes for legitimately huge rows')
}

server.close()
session.close()
