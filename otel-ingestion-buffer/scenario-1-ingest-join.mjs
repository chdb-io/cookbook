// Scenario 1 — the core pipeline: S3 (length-delimited protobuf) → enrich → ClickHouse,
// with ZERO row data ever passing through JavaScript.
//
// The payload already sits on S3 as length-delimited protobuf, keyed by entity
// (s3://…/{projectId}/observation/{eventBodyId}/*.pb). There is no HTTP request
// body to stream and no `JSON.parse` — the data is pulled from S3 by the engine.
// Per entity (one queue job), JavaScript only hands the engine a glob path:
//
//   s3('…/{eventBodyId}/*.pb','Protobuf')              read + parse protobuf      [engine, multi-thread]
//        │  GROUP BY id, argMaxIf(col, ts, col<>'')     field-level merge          [engine]
//        │     ── collapses the create + update partial events into one full row,
//        │        replacing the app-side read-modify-write entirely
//        ▼
//   dictGet(prompt) + match(model) + dictGet(price)     enrich (lookups)           [engine; dicts ← Postgres]
//        │  bpe_count(text, tokenizer)                  token count                [engine; WASM UDF]
//        ▼
//   INSERT INTO FUNCTION remoteSecure(...)              export                     [engine, native protocol]
//
// Real here (runs today on chdb-node 3.1.0-rc.2): protobuf read, the per-entity
// glob, the SQL field-merge, dictGet/match enrichment. Stubbed for the PoC: the
// token UDF (SQL placeholder → WASM), the dict source (local table → Postgres),
// and the export target (local table → remoteSecure). See ../langfuse_chdb_final_design_en.md.
//
// Run:  node scenario-1-ingest-join.mjs

import { rmSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import chdb from 'chdb'
import { PREFIX, PROJECT, entityGlob, entityInsertSQL, seedEntity, setupEngine } from './_shared.mjs'

const DATA = join(dirname(fileURLToPath(import.meta.url)), 'pb-data') // simulates the S3 bucket; gitignored
rmSync(DATA, { recursive: true, force: true })

// ---------------------------------------------------------------------------
// 1. Produce the S3 data: each observation arrives as create (input+model+prompt)
//    then update (output only), in SEPARATE .pb files — partial deltas, like the SDK.
// ---------------------------------------------------------------------------
const entities = [
  { id: 'obs_1', model: 'gpt-4o-2024-08-06', prompt: ['summarize', 3], input: 'Summarize this article: '.repeat(40), output: 'The article argues that '.repeat(8) },
  { id: 'obs_2', model: 'claude-fable-5',    prompt: ['classify', 1],  input: 'Classify the sentiment of: '.repeat(20), output: 'positive' },
  { id: 'obs_3', model: 'gpt-4o-2024-08-06', prompt: ['extract', 2],   input: 'Extract entities from: '.repeat(60), output: '' /* still running, no output yet */ },
]
for (const e of entities) seedEntity(DATA, e)
console.log(`generated protobuf for ${entities.length} entities under ${PREFIX}/${PROJECT}/observation/*/*.pb\n`)

// ---------------------------------------------------------------------------
// 2. Engine setup + per-entity processing. One job = one chDB statement; JS only
//    builds the glob path string. Production: file(...) → s3('s3://…'), and the
//    target → INSERT INTO FUNCTION remoteSecure(...). Nothing else changes.
// ---------------------------------------------------------------------------
const session = new chdb.Session()
setupEngine(session)
for (const e of entities) session.query(entityInsertSQL(entityGlob(DATA, e.id)))
console.log(`processed ${entities.length} entity jobs (each = one chDB glob → merge → enrich → export pass)\n`)

// ---------------------------------------------------------------------------
// 3. Verify: rows are merged + enriched, with ZERO JSON.parse / field-merge in JS.
// ---------------------------------------------------------------------------
console.log(session.query(`
  SELECT id, model, prompt_id, model_id, input_tokens, output_tokens, round(total_cost,6) AS total_cost
  FROM events_priced ORDER BY id`, 'PrettyCompact'))

const survived = JSON.parse(
  session.query(`SELECT count() AS n FROM events_priced WHERE input_tokens > 0`, 'JSONEachRow')).n
console.log(`\nfield-merge check  : ${survived}/${entities.length} rows kept input from the create event`,
  '(argMaxIf in SQL — the create+update partials were merged engine-side, no read-modify-write in JS)')
console.log('obs_3 (no output yet):',
  session.query(`SELECT output_tokens FROM events_priced WHERE id='obs_3'`, 'CSV').trim(),
  'output tokens — partial entity handled; its later update merges via the destination engine (scenario 3)')

session.close()
rmSync(DATA, { recursive: true, force: true })
