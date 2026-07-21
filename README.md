# chDB Cookbook

Runnable notebooks and code samples for [chDB](https://github.com/chdb-io/chdb) — the in-process OLAP SQL engine powered by ClickHouse.

Short, focused, runnable examples that show one technique each — organized by **goal** (build an agent, deploy an analyst, migrate, ingest), not by API surface. This page is the map: what each recipe is for, and how the stories relate.

## The map

chDB is one thing — a full ClickHouse query engine inside your process (`pip install chdb`). The recipes are four ways to put it to work:

```
                        ┌──────────────────────────────────────────┐
                        │   chDB — in-process ClickHouse (pip)       │
                        │   one engine: local files · S3 · Postgres  │
                        │   · MySQL · ClickHouse · DataFrames        │
                        └───────────────────┬────────────────────────┘
        ┌───────────────────────┬───────────┴───────────┬───────────────────────┐
        ▼                       ▼                       ▼                       ▼
 ┌───────────────┐     ┌───────────────┐       ┌───────────────┐       ┌───────────────┐
 │ GIVE AN AGENT │     │ DEPLOY AN     │       │ MIGRATE       │       │ INGEST &      │
 │ SQL HANDS     │     │ ANALYST       │       │ TO chDB       │       │ BUFFER        │
 │ (a tool in    │     │ (serverless — │       │               │       │               │
 │  your agent)  │     │  the ladder)  │       │               │       │               │
 └──────┬────────┘     └──────┬────────┘       └──────┬────────┘       └──────┬────────┘
        │                     │                       │                       │
 agent-framework-chdb  serverless-analyst      migration-from-duckdb   otel-ingestion-buffer
 dspy-chdb             aws-lambda · gcp-cloud-run
 dynamic-workflows     azure-container-apps · lambda-microvms
        │                     │
        └──── the SAME chdb.agents.ChDBTool ────┘
     build the agent with any framework  ×  deploy it on any tier
```

The two agent columns are **orthogonal and compose**: the `execute_sql` tool the deploy recipes give their analyst is the *same* `chdb.agents.ChDBTool` the framework recipes wrap. Pick a framework to build with, pick a tier to run on — independently. Migration and ingestion are the *data-in* side and feed any of the above.

## Recipes by goal

### Give an agent SQL hands — chDB as a tool in your framework
The engine runs inside the agent's process, so the agent needs exactly one tool. Same `chdb.agents.ChDBTool` underneath; one adapter per framework.
- [chDB tools for Microsoft Agent Framework](agent-framework-chdb/README.md) — `FunctionTool`s over `chdb.agents.ChDBTool`; schemas come from the package, nothing hand-maintained.
- [chDB tools for DSPy](dspy-chdb/README.md) — typed callables for `dspy.ReAct`; one copyable file, no package needed.
- [Federated SQL for Claude Dynamic Workflows](dynamic-workflows/README.md) — give every subagent a federated chDB query that joins S3, Postgres, ClickHouse, an HTTP API, and a DataFrame in one statement, no server to stand up.

### Deploy a chDB analyst — serverless, and how far state lives
One app (`chdb-serverless`, the ~50-line analyst); the store seam moves it up the [statefulness ladder](#the-serverless-statefulness-ladder).
- [One analyst, three clouds](serverless-analyst/README.md) — **the series hub**: the `chdb-serverless` package as one image on AWS Lambda, Cloud Run, and Azure Container Apps, cold-start economics side by side, and where stateful lives.
- [on AWS Lambda](aws-lambda/README.md) — classic Lambda container function: per-request billing, Function URL (IAM-auth by default).
- [on Google Cloud Run](gcp-cloud-run/README.md) — scale-to-zero container: idle = free, private by default.
- [on Azure Container Apps](azure-container-apps/README.md) — scale-to-zero: server-side ACR build, internal ingress by default.
- [on AWS Lambda MicroVMs](lambda-microvms/README.md) — a **private, warm** analyst per user: snapshot-hot starts, suspend/resume with memory intact, one Firecracker MicroVM per session.

### Migrate to chDB
- [Migrating from DuckDB to chDB](migration-from-duckdb/README.md) — runnable companion to the migration guide: the analyzer and an 18-query benchmark, including where a chDB `MergeTree` design choice trades off vs DuckDB.

### Ingest & buffer
- [OTEL ingestion buffer in Node.js](otel-ingestion-buffer/README.md) — chDB as an off-heap ingestion buffer: read length-delimited protobuf off S3, enrich, and export to ClickHouse over the native protocol in one SQL statement — the rows never pass through JavaScript.

## The serverless statefulness ladder

The deploy recipes are one app; **one line — the store seam — picks how far state survives.** Climb a rung only when the use case needs it.

```
  L3  AGENT MEMORY        CHDB_STORE=memory:      recall / reflect / forget       agent-memory           [future]
        ▲ adds memory semantics, built on L2
  L2  DURABLE OBJECT      CHDB_STORE=durable:     your data as an object in        durable-analytical-    [future]
        ▲ portable + your own storage             storage YOU own; portable        object
                                                  across clouds; ns.scan()
  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
  PLATFORM SNAPSHOT       local: + platform       whole VM/sandbox frozen &        lambda-microvms
        ▲ survives suspend/resume                 thawed; state on one host,       (e2b-sandbox, GKE/ACA
          (platform-managed, single host)         in the platform's storage        snapshots)
  L1  STATELESS           CHDB_STORE=local:       nothing — dies with the          aws-lambda · gcp-cloud-run
                                                  instance (bake data, scale 0)    · azure-container-apps
```

> **Platform snapshot is not a store tier** — it still runs the `local:` tier and lets the *platform* snapshot the whole box (Firecracker snapshot, E2B `pause`, GKE Pod snapshot). L2/L3 are different: the store seam moves the state *out* into storage you own, so it is portable and federatable across objects. Use a snapshot when one platform is enough; use a durable object when you must move across hosts/clouds, own the storage, or query across many objects.

## Getting started

```bash
pip install chdb jupyter
git clone https://github.com/chdb-io/cookbook
cd cookbook
jupyter lab
```

Every recipe runs end-to-end with `pip install chdb` and the dependencies listed in its README/first cell — no external services required (except where a recipe explicitly federates to ClickHouse Cloud or deploys to a cloud).

## Conventions

- Recipes are self-contained — the first step installs everything, the last prints expected output.
- All datasets are public (S3 open buckets, Hugging Face, or pip-installed fixtures).
- Each recipe has a 1-paragraph "What you'll learn" header and a "Try next" footer.
- File names use kebab-case; categories are goals, not API surfaces.

## Contributing

PRs welcome. To propose a recipe:

1. Open an issue with the working title and a one-paragraph summary of what it teaches.
2. Place it under the matching goal, and add a one-line entry to the map above.
3. Ensure it runs top-to-bottom on a clean `pip install chdb` environment.

For larger contributions (new categories, multi-recipe tutorials), please discuss in [Discord](https://discord.gg/D2Daa2fM5K) first.

## License

Apache 2.0 — see [LICENSE](LICENSE).

## Related

- Main chDB repository: https://github.com/chdb-io/chdb
- chDB documentation: https://clickhouse.com/docs/chdb
- LLM-friendly index: https://clickhouse.com/docs/chdb/llms.txt
- Awesome chDB: https://github.com/chdb-io/awesome-chdb
- Community: https://discord.gg/D2Daa2fM5K
