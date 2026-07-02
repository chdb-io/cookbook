// Shared scaffolding for the OTEL ingestion cookbook.
//
// The payload already sits on S3 as length-delimited protobuf, keyed by entity
// (s3://…/{projectId}/observation/{eventBodyId}/*.pb). Per entity, JavaScript
// only hands chDB a glob path; ONE SQL statement reads + parses the protobuf,
// field-merges the create/update partial events (argMaxIf … GROUP BY id),
// enriches via dictGet (prompt/price) + match() (model) + a token UDF, and
// exports — no row data, no JSON.parse, no field-merge in JavaScript.
//
// Sync vs async: one-time startup DDL uses the synchronous `session.query` (it
// runs once at boot, blocking there is fine). The per-entity hot path runs as
// `await session.queryAsync(...)` so the engine executes off a worker thread and
// the Node event loop is never frozen while a job runs.
//
// Demo substitutions (production form in comments at each call site):
//   • file('…/*.pb','Protobuf')        → s3('s3://…/*.pb','Protobuf')
//   • bpe_count SQL placeholder        → CREATE FUNCTION bpe_count LANGUAGE WASM …
//   • dict SOURCE(CLICKHOUSE(TABLE …)) → SOURCE(POSTGRESQL(…))
//   • INSERT INTO events_priced (local)→ INSERT INTO FUNCTION remoteSecure(…)

import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import protobuf from 'protobufjs'

const here = dirname(fileURLToPath(import.meta.url))
export const PROTO = join(here, 'observation.proto')
export const PREFIX = 'events'
export const PROJECT = 'proj_demo'

// keepCase:true so JS field names stay snake_case (match the .proto and what the
// engine maps); otherwise protobufjs camelCases project_id → projectId and
// silently drops snake_case values.
const { root } = protobuf.parse(readFileSync(PROTO, 'utf8'), { keepCase: true })
export const Obs = root.lookupType('Observation')

// Write one length-delimited protobuf event under the entity prefix.
// encodeDelimited = varint length prefix + message == ClickHouse `Protobuf` framing.
export function writeEvent(dataDir, eventBodyId, eventId, fields) {
  const dir = join(dataDir, PREFIX, PROJECT, 'observation', eventBodyId)
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, `${eventId}.pb`), Obs.encodeDelimited(Obs.create(fields)).finish())
}

// Seed an observation as a create event (input/model/prompt) then, if it has
// output, an update event carrying ONLY output at a later ts — i.e. partial
// deltas, exactly like the SDK protocol (create then update).
export function seedEntity(dataDir, e) {
  writeEvent(dataDir, e.id, 'evt-create', {
    id: e.id, ts: e.ts ?? 1000, project_id: PROJECT, trace_id: `trace_${e.id}`,
    input: e.input, model: e.model, prompt_name: e.prompt[0], prompt_version: e.prompt[1],
  })
  if (e.output) writeEvent(dataDir, e.id, 'evt-update', { id: e.id, ts: (e.ts ?? 1000) + 5, output: e.output })
}

// The glob for one entity's files. Production: s3('s3://bucket/…/{eventBodyId}/*.pb').
export function entityGlob(dataDir, eventBodyId) {
  return join(dataDir, PREFIX, PROJECT, 'observation', eventBodyId, '*.pb')
}

// Engine-side scaffolding: destination table, reference dictionaries, token UDF.
// destEngine lets a scenario pick ReplacingMergeTree (full rows) vs other engines.
export function setupEngine(session, { destEngine = 'ReplacingMergeTree(event_ts)' } = {}) {
  // Destination. Production: remoteSecure(...) into CH Cloud, where the
  // cross-chunk / cross-time merge is the destination engine's job:
  //   full rows → ReplacingMergeTree(event_ts);  partial → AggregatingMergeTree + argMaxIf
  session.query(`
    CREATE TABLE events_priced (
      id String, project_id String, trace_id String, model String,
      prompt_id String, model_id String,
      input_tokens UInt64, output_tokens UInt64, total_cost Float64,
      event_ts UInt64
    ) ENGINE = ${destEngine} ORDER BY (project_id, id)`)

  // Reference data. Production: dictionaries are SOURCE(POSTGRESQL(...)), refreshed
  // by LIFETIME — never queried per row, and they REPLACE Redis on this path
  // (the dict is the cache over PG, the source of truth). Freshness:
  //   prompt → complex_key_cache (read-through; a new version resolves on miss)
  //   model/price → hashed + LIFETIME (slow-changing, periodic refresh)
  session.query(`CREATE TABLE prompt_src (project_id String, prompt_name String, prompt_version UInt32, id String) ENGINE=MergeTree ORDER BY (project_id, prompt_name, prompt_version)`)
  session.query(`INSERT INTO prompt_src VALUES
    ('${PROJECT}','summarize',3,'prompt_sum_v3'),
    ('${PROJECT}','classify',1,'prompt_cls_v1'),
    ('${PROJECT}','extract',2,'prompt_ext_v2')`)
  session.query(`
    CREATE DICTIONARY prompt_dict (project_id String, prompt_name String, prompt_version UInt32, id String)
    PRIMARY KEY project_id, prompt_name, prompt_version
    SOURCE(CLICKHOUSE(TABLE 'prompt_src'))     -- prod: SOURCE(POSTGRESQL(host … table 'prompts'))
    LAYOUT(COMPLEX_KEY_CACHE(SIZE_IN_CELLS 100000)) LIFETIME(MIN 30 MAX 60)`)

  // model resolution = regex match on the provided model name → internal id + tokenizer
  // (regexp_tree dictionary is the optimized form; a small table + match() is the simple equivalent)
  session.query(`CREATE TABLE models (pattern String, model_id String, tokenizer String) ENGINE=MergeTree ORDER BY pattern`)
  session.query(`INSERT INTO models VALUES ('^gpt-4o','m_gpt4o','o200k_base'), ('^claude','m_claude','claude_bpe')`)

  session.query(`CREATE TABLE price_src (model_id String, in_micro UInt64, out_micro UInt64) ENGINE=MergeTree ORDER BY model_id`)
  // micro-USD per 1M tokens
  session.query(`INSERT INTO price_src VALUES ('m_gpt4o',2500000,10000000), ('m_claude',3000000,15000000)`)
  session.query(`
    CREATE DICTIONARY price_dict (model_id String, in_micro UInt64, out_micro UInt64)
    PRIMARY KEY model_id
    SOURCE(CLICKHOUSE(TABLE 'price_src'))       -- prod: SOURCE(POSTGRESQL(… table 'pricing'))
    LAYOUT(COMPLEX_KEY_HASHED()) LIFETIME(MIN 30 MAX 60)`)

  // Token count. PLACEHOLDER (length/4). Production: a WASM UDF (tiktoken-rs → wasm,
  // vocab embedded) — runs as parallel engine instances, off JS. Requires chdb-core
  // to enable the upstream WASM-UDF runtime (today: SUPPORT_IS_DISABLED).
  //   CREATE FUNCTION bpe_count LANGUAGE WASM ARGUMENTS (text String, tokenizer String)
  //     RETURNS UInt64 FROM 'tiktoken.wasm' SHA256_HASH '…';
  session.query(`CREATE FUNCTION bpe_count AS (text, tokenizer) -> toUInt64(length(text) / 4 + 1)`)
}

// The canonical per-entity statement: glob → field-merge → enrich → export.
// This is THE pipeline; every scenario uses it. Production swaps file()→s3() and
// the target → FUNCTION remoteSecure(...). `extraSettings` appends to SETTINGS.
export function entityInsertSQL(glob, { target = 'events_priced', extraSettings = '' } = {}) {
  const settings = `format_schema = '${PROTO}:Observation'` + (extraSettings ? `, ${extraSettings}` : '')
  return `
    INSERT INTO ${target}
    WITH merged AS (
      SELECT
        id,
        argMaxIf(project_id,     ts, project_id     != '') AS project_id,
        argMaxIf(trace_id,       ts, trace_id       != '') AS trace_id,
        argMaxIf(input,          ts, input          != '') AS input,
        argMaxIf(output,         ts, output         != '') AS output,
        argMaxIf(model,          ts, model          != '') AS model,
        argMaxIf(prompt_name,    ts, prompt_name    != '') AS prompt_name,
        argMaxIf(prompt_version, ts, prompt_version != 0)  AS prompt_version,
        max(ts) AS event_ts
      FROM file('${glob}', 'Protobuf')          -- prod: s3('s3://…/*.pb','Protobuf')
      GROUP BY id                               -- merge ALL fields by id (create+update are disjoint)
    )
    SELECT
      m.id, m.project_id, m.trace_id, m.model,
      dictGet('prompt_dict', 'id', (m.project_id, m.prompt_name, m.prompt_version))   AS prompt_id,
      md.model_id                                                                     AS model_id,
      bpe_count(m.input,  md.tokenizer)                                               AS input_tokens,
      bpe_count(m.output, md.tokenizer)                                               AS output_tokens,
      input_tokens  * dictGet('price_dict','in_micro',  tuple(md.model_id)) / 1e6
        + output_tokens * dictGet('price_dict','out_micro', tuple(md.model_id)) / 1e6 AS total_cost,
      m.event_ts
    FROM merged AS m, models AS md              -- model regex match (prod: regexp_tree dict)
    WHERE match(m.model, md.pattern)
    SETTINGS ${settings}`
}
