// Scenario 2 — flow & resource control in the pull model.
//
// In the pull design there is no HTTP body to backpressure: the engine reads the
// S3 chunk itself, and JavaScript only holds a path string. So "flow control"
// here is three different things from the old streaming model:
//
//   A. Bounded JS memory      — JS never holds row data, so heap stays flat no
//                               matter how big the entity's files are.
//   B. Bounded per-job cost   — cap a single entity job with engine SETTINGS
//                               (max_memory_usage, max_threads); a too-low cap
//                               fails with a typed error you can handle.
//   C. Throughput / scale-out — one chDB session serializes queries (per-process
//                               singleton), each internally multi-threaded; you
//                               scale by adding processes (Fargate tasks), not threads.
//
// Run:  node scenario-2-flow-control.mjs   (add --expose-gc for an exact heap delta)

import { rmSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import chdb from 'chdb'
import { entityGlob, entityInsertSQL, seedEntity, setupEngine } from './_shared.mjs'

const DATA = join(dirname(fileURLToPath(import.meta.url)), 'pb-data')
rmSync(DATA, { recursive: true, force: true })

const session = new chdb.Session()
setupEngine(session)

// A heavy entity (~2 MB of input text) plus a few normal ones.
seedEntity(DATA, { id: 'big', model: 'gpt-4o-2024-08-06', prompt: ['summarize', 3], input: 'x'.repeat(2_000_000), output: 'done' })
for (let i = 0; i < 8; i++) {
  seedEntity(DATA, { id: `obs_${i}`, model: 'gpt-4o-2024-08-06', prompt: ['extract', 2], input: `payload ${i} `.repeat(50), output: 'ok' })
}

// ---------------------------------------------------------------------------
// A. Bounded JS memory — process the 2 MB entity; the V8 heap barely moves
//    because the data lives off-heap in the engine, not in JavaScript.
// ---------------------------------------------------------------------------
global.gc?.()
const before = process.memoryUsage().heapUsed
await session.queryAsync(entityInsertSQL(entityGlob(DATA, 'big')))
global.gc?.()
const after = process.memoryUsage().heapUsed
console.log('A. bounded memory :', `JS heap Δ ${((after - before) / 1024).toFixed(1)} KiB while ingesting a 2 MB entity`,
  '(data stayed off the V8 heap; JS only passed a path string)')

// ---------------------------------------------------------------------------
// B. Bounded per-job cost — a deliberately tiny memory cap fails with a typed
//    error (handle it: raise the cap / shrink the chunk); a sane cap succeeds.
// ---------------------------------------------------------------------------
try {
  await session.queryAsync(entityInsertSQL(entityGlob(DATA, 'big'), { extraSettings: 'max_memory_usage = 1' }))
  console.log('B. resource cap   : (unexpected) tiny cap did not trip')
} catch (e) {
  const limited = /MEMORY_LIMIT_EXCEEDED|Memory limit/i.test(e.message)
  console.log('B. resource cap   :', limited ? 'typed MEMORY_LIMIT_EXCEEDED at max_memory_usage=1 → raise the cap or split the chunk' : `error: ${e.message.slice(0, 80)}`)
}
await session.queryAsync(entityInsertSQL(entityGlob(DATA, 'big'), { extraSettings: 'max_memory_usage = 2000000000, max_threads = 4' }))
console.log('                    sane cap (2 GB, 4 threads) → ok; one job\'s footprint is bounded by SETTINGS')

// ---------------------------------------------------------------------------
// C. Throughput / scale-out — one session runs queries one at a time (each
//    internally multi-threaded). Wall-clock = sum of jobs; you scale by running
//    more processes (Fargate tasks), each its own session, all writing the same CH.
// ---------------------------------------------------------------------------
const t0 = Date.now()
for (let i = 0; i < 8; i++) await session.queryAsync(entityInsertSQL(entityGlob(DATA, `obs_${i}`)))
console.log('C. throughput     :', `8 entity jobs serialized through one session in ${Date.now() - t0} ms`,
  '(one chDB per process; scale by adding Fargate tasks, not worker threads)')

console.log('\nentities exported :', session.query('SELECT uniqExact(id) FROM events_priced', 'CSV').trim(),
  '(big + obs_0..7; re-inserts of "big" dedup by id via the destination RMT)')
session.close()
rmSync(DATA, { recursive: true, force: true })
