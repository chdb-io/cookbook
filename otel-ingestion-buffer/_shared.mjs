// Shared helpers for the three OTEL-ingestion-buffer scenarios.
//
// Each scenario file (scenario-1/2/3) is meant to be read and run on its own;
// this module holds only the boilerplate they all need — the buffer schema, a
// byte-level field peek, and a synthetic Langfuse-style span generator — so the
// scenario files can stay focused on the one thing they teach.

// ---------------------------------------------------------------------------
// Schema: a Null table as the ingest funnel + a materialized view that enriches
// in engine threads. The Null table stores nothing; inserting into it only
// triggers the view, which extracts/computes/truncates without JS ever parsing
// the payload. ns timestamps are stored as UInt64 (they arrive as JSON strings,
// so the full uint64 survives — JS number would lose precision past 2^53).
// ---------------------------------------------------------------------------
export function createSchema(session) {
  session.query(`
    CREATE TABLE IF NOT EXISTS otel_raw (
      trace_id   String,
      span_id    String,
      start_ns   UInt64,
      end_ns     UInt64,
      name       String,
      attributes String
    ) ENGINE = Null`)

  session.query(`
    CREATE TABLE IF NOT EXISTS events (
      trace_id      String,
      span_id       String,
      start_ns      UInt64,
      duration_ms   Float64,
      name          String,
      model         String,
      input_tokens  UInt64,
      output_tokens UInt64,
      prompt_preview String,
      tenant        LowCardinality(String),
      received_at   DateTime64(9) DEFAULT now64(9)
    ) ENGINE = MergeTree ORDER BY (tenant, trace_id, span_id)`)

  session.query(`
    CREATE MATERIALIZED VIEW IF NOT EXISTS mv_events TO events AS
    SELECT
      trace_id, span_id, start_ns,
      (end_ns - start_ns) / 1e6                                       AS duration_ms,
      name,
      JSONExtractString(attributes, 'gen_ai.request.model')          AS model,
      JSONExtractUInt(attributes, 'gen_ai.usage.input_tokens')       AS input_tokens,
      JSONExtractUInt(attributes, 'gen_ai.usage.output_tokens')      AS output_tokens,
      leftUTF8(JSONExtractString(attributes, 'gen_ai.prompt'), 80)   AS prompt_preview,
      JSONExtractString(attributes, 'tenant.id')                     AS tenant
    FROM otel_raw`)
}

// ---------------------------------------------------------------------------
// Lightweight byte-level field peek — the "SAX" lane. When the service itself
// needs one small field (tenant routing, a sampling/quota key), scan the bytes
// for it instead of materializing the batch as JS objects. No object tree, early
// exit on the first hit; for a single-tenant batch that is O(1) per request.
//
// Handles both a top-level `"key":"value"` and the escaped `\"key\":\"value\"`
// form you get when the field lives inside a JSON-encoded string column (as OTEL
// attributes usually do). For deeply nested / per-line extraction at scale, swap
// this for a streaming SAX parser (stream-json, clarinet); the principle is the
// same — materialize only the fields you filter for.
// ---------------------------------------------------------------------------
export function peekField(buf, key) {
  let needle = Buffer.from(`"${key}":"`)
  let i = buf.indexOf(needle)
  if (i >= 0) {
    const start = i + needle.length
    let end = start
    while (end < buf.length && buf[end] !== 0x22 /* " */) {
      if (buf[end] === 0x5c /* \ */) end++ // skip the escaped pair
      end++
    }
    return buf.toString('utf8', start, end)
  }
  needle = Buffer.from(`\\"${key}\\":\\"`)
  i = buf.indexOf(needle)
  if (i < 0) return undefined
  const start = i + needle.length
  let end = start
  while (end < buf.length) {
    if (buf[end] === 0x5c /* \ */) {
      if (buf[end + 1] === 0x22 /* " */) break // \" closes the nested string
      end += 2
    } else end++
  }
  return buf.toString('utf8', start, end)
}

// ---------------------------------------------------------------------------
// Synthetic Langfuse-style LLM span: wide attributes with the full prompt
// inline, ns-precision timestamps carried as strings so the uint64 survives.
// ---------------------------------------------------------------------------
const PROMPT = 'Explain the difference between columnar and row-oriented storage, with examples. '.repeat(20)
const MODELS = ['gpt-4o', 'claude-fable-5']

export function span(i, tenant) {
  const startNs = 1780000000000000000n + BigInt(i) * 1000000n
  return JSON.stringify({
    trace_id: `trace-${String(i).padStart(6, '0')}`,
    span_id: `span-${String(i).padStart(6, '0')}`,
    start_ns: String(startNs),
    end_ns: String(startNs + BigInt(50_000_000 + (i % 7) * 10_000_000)),
    name: 'llm.call',
    attributes: JSON.stringify({
      'gen_ai.request.model': MODELS[i % MODELS.length],
      'gen_ai.prompt': PROMPT,
      'gen_ai.usage.input_tokens': 420 + (i % 1000),
      'gen_ai.usage.output_tokens': 120 + (i % 500),
      'tenant.id': tenant,
    }),
  })
}

// A lazy generator of NDJSON span lines (one span per line, '\n' = row
// boundary). Feed it to Readable.from() — as an HTTP request body or as a
// streaming insert source — so the batch is produced on demand and never
// assembled in memory.
export function* spanLines(from, to, tenant) {
  for (let i = from; i < to; i++) yield span(i, tenant) + '\n'
}
