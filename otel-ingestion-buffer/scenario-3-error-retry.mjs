// Scenario 3 — failures & retries in the pull model.
//
// The unit of work is one per-entity SQL statement (glob → merge → enrich →
// export). Three failure modes, and why each is safe:
//
//   1. Export fails (remoteSecure down)  → just re-run the same statement.
//      Idempotent: the destination ReplacingMergeTree dedups by (id, event_ts),
//      and the merge is order-independent — re-runs and concurrent writers from
//      multiple Fargate tasks converge to one row. No app-side lock.
//   2. Corrupt chunk (bad .pb)           → that entity's job throws a typed parse
//      error and is isolated; other entities' jobs are unaffected. Quarantine /
//      retry just that one.
//   3. Late-arriving update              → an update file shows up after the row
//      was already exported. Re-processing the entity emits a higher-event_ts
//      merged row; the destination engine keeps the latest. No read-back.
//
// Run:  node scenario-3-error-retry.mjs

import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import chdb from 'chdb'
import { PROJECT, entityGlob, entityInsertSQL, seedEntity, setupEngine, writeEvent } from './_shared.mjs'

const DATA = join(dirname(fileURLToPath(import.meta.url)), 'pb-data')
rmSync(DATA, { recursive: true, force: true })

const session = new chdb.Session()
setupEngine(session) // destination = ReplacingMergeTree(event_ts)

// ---------------------------------------------------------------------------
// 1. Export failure → idempotent re-run. Process the same entity twice (a retry
//    after a failed export). The destination collapses the duplicate by event_ts.
// ---------------------------------------------------------------------------
seedEntity(DATA, { id: 'retry_me', model: 'gpt-4o-2024-08-06', prompt: ['summarize', 3], input: 'hello', output: 'world' })
await session.queryAsync(entityInsertSQL(entityGlob(DATA, 'retry_me')))
await session.queryAsync(entityInsertSQL(entityGlob(DATA, 'retry_me'))) // the retry
const raw = session.query("SELECT count() FROM events_priced WHERE id='retry_me'", 'CSV').trim()
const final = session.query("SELECT count() FROM events_priced FINAL WHERE id='retry_me'", 'CSV').trim()
console.log('1. retry safe     :', `${raw} raw rows → ${final} after FINAL (ReplacingMergeTree dedup by id+event_ts; re-run is a no-op)`)

// ---------------------------------------------------------------------------
// 2. Corrupt chunk → typed error, isolated to that entity. A bad .pb file under
//    one entity fails only that job; a healthy entity processes fine.
// ---------------------------------------------------------------------------
const badDir = join(DATA, 'events', PROJECT, 'observation', 'corrupt')
mkdirSync(badDir, { recursive: true })
writeFileSync(join(badDir, 'evt.pb'), Buffer.from('this is not a valid protobuf message at all'))
try {
  await session.queryAsync(entityInsertSQL(entityGlob(DATA, 'corrupt')))
  console.log('2. corrupt chunk  : (unexpected) corrupt .pb did not error')
} catch (e) {
  console.log('2. corrupt chunk  :', `typed parse error → quarantine/retry just this entity (${e.message.split('\n')[0].slice(0, 70)}…)`)
}
seedEntity(DATA, { id: 'healthy', model: 'claude-fable-5', prompt: ['classify', 1], input: 'fine', output: 'ok' })
await session.queryAsync(entityInsertSQL(entityGlob(DATA, 'healthy')))
console.log('                    healthy entity exported fine — the bad chunk did not affect other jobs')

// ---------------------------------------------------------------------------
// 3. Late-arriving update → re-process; the destination keeps the latest version.
// ---------------------------------------------------------------------------
writeEvent(DATA, 'late', 'evt-create', { id: 'late', ts: 2000, project_id: PROJECT, trace_id: 'trace_late', input: 'q', model: 'gpt-4o-2024-08-06', prompt_name: 'extract', prompt_version: 2 })
await session.queryAsync(entityInsertSQL(entityGlob(DATA, 'late')))
const beforeOut = session.query("SELECT output_tokens FROM events_priced FINAL WHERE id='late'", 'CSV').trim()
// ...the update event lands later, in a new file...
writeEvent(DATA, 'late', 'evt-update', { id: 'late', ts: 2005, output: 'an answer' })
await session.queryAsync(entityInsertSQL(entityGlob(DATA, 'late'))) // re-process; merged row has higher event_ts
const afterOut = session.query("SELECT output_tokens FROM events_priced FINAL WHERE id='late'", 'CSV').trim()
console.log('3. late update    :', `output_tokens ${beforeOut} → ${afterOut} after the update arrived`,
  '(higher event_ts wins; order-independent, so concurrent task re-runs are safe)')

session.close()
rmSync(DATA, { recursive: true, force: true })
