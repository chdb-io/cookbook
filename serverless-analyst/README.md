# One analyst, three clouds — chDB on serverless

A [50-line data-analyst agent](../lambda-microvms/README.md#part-1--the-analyst-on-your-laptop) — Claude plus one `execute_sql` tool plus [chDB](https://github.com/chdb-io/chdb) (in-process ClickHouse) — packaged as **one serverless container** and deployed to each major cloud's serverless compute. The point of doing it three times is that almost nothing changes: the app is shared byte-for-byte, so the platforms become directly comparable — same workload, same image, real measured numbers.

> *One container image: ClickHouse-grade SQL, a Claude analyst, and 1M rows of data. Pick a lane.*

## The lanes, measured

| Lane | Cold start | Warm hit | Idle cost |
|---|---|---|---|
| [AWS Lambda](../aws-lambda/) | **9.9 s** | ~520 ms | zero (per-request billing) |
| [Google Cloud Run](../gcp-cloud-run/) | **16.2 s** | ~480 ms | zero (`min-instances 0`) |
| [Azure Container Apps](../azure-container-apps/) | **30.1 s** | ~500 ms | ~zero (`min-replicas 0`; registry pennies) |

All numbers measured on the exact code in this repository, with the 1M-row ClickBench store baked into a ~1 GB image. Cold start is dominated by how each platform moves that image — Lambda's on-demand chunk loading explains its lead. Each lane's README shows the levers when its cold start matters (paid-warm instances, a smaller bake, or querying object storage live).

## The app, shared across every lane

| File | Role | Sharing |
|---|---|---|
| `agent.py` | the 50-line analyst: Claude + one `execute_sql` tool | byte-identical in all three lanes (and in the MicroVMs recipe) |
| `main.py` | the HTTP app: `/health` `/query` `/ask` | byte-identical in all three lanes |
| `init_db.py` | build-time bake: 1M ClickBench rows → MergeTree | byte-identical in all three lanes |
| `Dockerfile` | two-stage image: bake, then copy the store in one layer | identical between Cloud Run and Container Apps; the Lambda lane adds **exactly one line** (the [Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter), inert everywhere else) |
| `deploy.sh` / `teardown.sh` | the only genuinely per-cloud code | ~50–60 lines each |

Baking the dataset into the image at build time is what makes scale-to-zero workable everywhere: a cold instance pulls the image and serves — no data download on boot.

## Where state lives

These lanes are **stateless by design**: materialized tables and the demo conversation are instance-local and die with the instance — the honest contract for a scale-to-zero SQL endpoint. Above that base sit two stateful tiers:

1. **Platform snapshot tiers** keep this same app stateful today, per cloud: AWS Lambda MicroVMs (the [companion recipe](../lambda-microvms/) — platform snapshots, suspend/resume with memory and disk intact, one private analyst per user), GKE Agent Sandbox with Pod snapshots on Google Cloud, and Azure Container Apps Sandboxes on Azure.
2. **Truly persistent per-user analysts** — the engine's state living as a **durable object on S3-compatible storage**, portable across clouds — are the planned **2.0** of this series (`chdb.durable`).

## Adapt the series to your workload

- **Your data:** edit the `CREATE TABLE ... AS SELECT` in any lane's `init_db.py` — anything chDB can read works (S3/HTTP parquet & 80+ formats, `postgresql()`, `mysql()`, `iceberg()`, `deltaLake()`). Rebuild to rebake.
- **Live external data:** the analyst's `execute_sql` tool reaches `s3()`, `postgresql()`, and `remoteSecure()` to ClickHouse Cloud at query time — the baked store is a cache, not a cage.
- **Concurrency:** chDB sessions are single-writer, so `main.py` serializes engine access behind a lock; scale out with more instances rather than more threads. Each lane's README shows its platform's knobs.
- **Secrets:** every lane passes `ANTHROPIC_API_KEY` as a plain env var for the demo; each README names its platform's secret store for anything real.
