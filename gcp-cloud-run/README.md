# A data analyst agent with chDB — on Google Cloud Run

The Google Cloud lane of the [serverless analyst series](../serverless-analyst/): the [`chdb-serverless`](https://pypi.org/project/chdb-serverless/) analyst as a **scale-to-zero container** on [Cloud Run](https://cloud.google.com/run). The app is `pip install chdb-serverless`; deployment is the [shared image](../serverless-analyst/#the-image) plus one gcloud command.

## Why Cloud Run for this

| chDB property | Cloud Run property |
|---|---|
| Engine + data live inside the process | Everything ships in one image — no database to provision |
| The store is baked at build time | A cold instance pulls the image and serves — no data download on boot |
| ~10 ms aggregations over 1M rows in-process | `min-instances: 0` means idle = free |
| One engine per instance | The platform adds instances under load — each with its own engine |

## Deploy

The hardened Cloud Run deploy/teardown scripts live with the package:

```bash
git clone https://github.com/chdb-io/chdb-lambda && cd chdb-lambda
export ANTHROPIC_API_KEY=sk-...    # optional — omit for /query only
deploy/gcp-cloud-run/deploy.sh     # us-central1 by default; REGION=... to override
```

Measured on the published package: image build ~2–3 min, **~16 s cold start** (after 15 min idle), **~450–520 ms warm**, `/query` ~85 ms.

## Cloud Run specifics (handled by the deploy script)

- **Auth**: deploys **private by default** and calls with an identity token; `PUBLIC=1` opts into a public URL. `/query` runs arbitrary SQL and chDB's `url()`/`s3()` can reach the metadata server, so private is the safe default.
- **Concurrency**: `--concurrency` bounds the per-instance queue behind the engine lock; `--max-instances` fans out. More instances beat more threads for read-heavy traffic.
- **Secrets**: the API key goes into Secret Manager (via stdin) and is referenced with `--set-secrets`, never passed in argv.

## Try next

- The same package on [AWS Lambda](../aws-lambda/) and [Azure Container Apps](../azure-container-apps/) — [compare the measured economics](../serverless-analyst/#the-lanes-measured).
- Stateful per-user analysts — state as a durable object on S3, portable across clouds — are the planned **2.0** (`chdb-serverless[durable]`).
