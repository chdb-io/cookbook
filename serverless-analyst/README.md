# One analyst, three clouds — chDB on serverless

A [50-line data-analyst agent](../lambda-microvms/README.md#part-1--the-analyst-on-your-laptop) — Claude plus one `execute_sql` tool plus [chDB](https://github.com/chdb-io/chdb) (in-process ClickHouse) — packaged as the [`chdb-serverless`](https://pypi.org/project/chdb-serverless/) library and deployed, **as one image**, to each major cloud's serverless compute. Almost nothing changes between clouds: the app is one pip install, so the platforms become directly comparable.

> *One `pip install`, one image: ClickHouse-grade SQL, a Claude analyst, and 1M rows of data. Pick a lane.*

## The package does the work

The app used to be copied into every recipe. It now lives in one place — the [`chdb-serverless`](https://github.com/chdb-io/chdb-lambda) package — so there's a single source of truth and nothing drifts:

```bash
pip install chdb-serverless             # the app: /health /query /ask
pip install "chdb-serverless[anthropic]"  # + the Claude-backed /ask endpoint
```

```python
from chdb_serverless import analyst_app, open_store
app = analyst_app()          # FastAPI: /health /query /ask
```

Two plug seams keep the core unchanged as you grow:

- **`CHDB_STORE`** — `local:` (this series, stateless) → `durable:` (S3-backed object) → `memory:` (analytical agent memory).
- **`CHDB_MODEL`** — `anthropic:…`, `openai:…`, `qwen:…`, or any OpenAI-compatible server via `@base_url`. The analyst is not anchored to one LLM.

## The image

One [`Dockerfile`](Dockerfile) here installs the published package, bakes the 1M-row ClickBench store, and serves it — the same image on all three clouds:

```bash
docker build -f serverless-analyst/Dockerfile -t chdb-analyst serverless-analyst
```

## Deploy

The production-hardened, per-cloud deploy scripts live with the package at [chdb-io/chdb-lambda `deploy/`](https://github.com/chdb-io/chdb-lambda/tree/main/deploy) (auth-safe defaults, idempotent, ownership-guarded teardown). Clone it and run one command:

```bash
git clone https://github.com/chdb-io/chdb-lambda && cd chdb-lambda
export ANTHROPIC_API_KEY=sk-...        # optional — omit for /query only
deploy/aws-lambda/deploy.sh            # or gcp-cloud-run/ or azure-container-apps/
```

## The lanes, measured

Cold start (scale-from-zero, ~1 GB image, 1M rows baked in), warm latency, and idle cost — measured with the published package deployed to each cloud:

| Lane | Cold start | Warm | Idle cost |
|---|---|---|---|
| [AWS Lambda](../aws-lambda/) | ~34 s | ~500 ms | zero (per-request) |
| [Google Cloud Run](../gcp-cloud-run/) | ~16 s | ~480 ms | zero (`min-instances 0`) |
| [Azure Container Apps](../azure-container-apps/) | ~20 s | ~500 ms | ~zero (`min-replicas 0`) |

Cold start is dominated by how each platform moves the image; the engine itself does a GROUP BY over 1M rows in ~33 ms in-process. Each lane's page has its specifics.

## Where state lives

These lanes are **stateless by design** — materialized tables and the demo conversation die with the instance, the honest contract for a scale-to-zero SQL endpoint. Above it: platform snapshot tiers keep the same app stateful today (AWS [Lambda MicroVMs](../lambda-microvms/), GKE Agent Sandbox Pod snapshots, Azure Container Apps Sandboxes), and portable per-user analysts — state as a durable object on S3 — are the planned **2.0** (`chdb-serverless[durable]`).
