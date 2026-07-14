# A data analyst agent with chDB in 50 lines — on Google Cloud Run

The Google Cloud lane of the [serverless analyst series](../serverless-analyst/): the same 50-line analyst — Claude plus one `execute_sql` tool plus [chDB](https://github.com/chdb-io/chdb) (in-process ClickHouse) — as a **scale-to-zero container** on [Cloud Run](https://cloud.google.com/run). The app and image are the series' shared ones (`agent.py`, `main.py`, `init_db.py`, `Dockerfile` — [what's shared and what isn't](../serverless-analyst/#the-app-shared-across-every-lane)); the ~50-line `deploy.sh` is the only Google-specific code.

## Why Cloud Run for this

| chDB property | Cloud Run property |
|---|---|
| Engine + data live inside the process | Everything ships in one image — no database to provision |
| The store can be baked at build time | A cold instance pulls the image and serves — no data download on boot |
| ~10 ms aggregations over 1M rows in-process | `min-instances: 0` means idle = free |
| One engine per instance, no shared server | The platform adds instances under load (`max-instances`) — each with its own engine |

State is instance-local and ephemeral by design — [where stateful lives in this series](../serverless-analyst/#where-state-lives) (on Google Cloud, the platform snapshot tier is GKE Agent Sandbox with Pod snapshots; the portable 2.0 is `chdb.durable`).

## Deploy

Prerequisites: `gcloud` authenticated against a project with billing; the script enables the `run`/`cloudbuild`/`artifactregistry` APIs if needed.

```bash
export ANTHROPIC_API_KEY=sk-...   # optional — omit to deploy /query only
./deploy.sh                       # us-central1 by default; REGION=... to override
```

`gcloud run deploy --source .` hands the directory to Cloud Build, which runs the Dockerfile — including the `init_db.py` stage that downloads and bakes the 1M-row ClickBench store — and rolls the image out at 2 vCPU / 4 GiB with `min-instances 0`. Measured on this exact code (us-central1; image built natively on an amd64 builder in **2m40s**):

```
==> service https://chdb-analyst-<project-number>.us-central1.run.app
==> warm hit: 458 ms
==> /health: {"status":"ok","engine":"chdb 4.2.1","baked_rows":1000000,"instance":"235dcf25",…}
```

Ask it something:

```bash
curl -s $URL/ask -H 'Content-Type: application/json' \
  -d '{"question": "Which regions drive the most traffic?"}'
# → "| 1 | 229 | 426,435 | 27,961 | …"  (turns: 2, ~9s — one Claude round-trip + in-process SQL)
```

## The scale-to-zero economics, measured

| Path | Time to first response |
|---|---|
| Warm instance | **~450–520 ms** wire |
| Cold start after scale-to-zero | **16.2 s** (measured after 15 min idle) — image pull dominates; the app itself reports 0.2 s of uptime at first response |

([How the other lanes compare.](../serverless-analyst/#the-lanes-measured)) The levers when 16 s matters: `--min-instances 1` trades idle compute for zero cold starts, `BAKE_PARTITIONS` shrinks the store and the pull, startup CPU boost narrows the gap, or keep the store out of the image and query object storage live (`s3()` over parquet).

## Cloud Run specifics

- **Auth (important):** `/query` runs caller-supplied ClickHouse SQL, and chDB's `url()`/`s3()` functions can reach the instance metadata server — a public endpoint is therefore an unauthenticated SSRF path to your service identity token. `deploy.sh` deploys **private by default** and calls with `gcloud auth print-identity-token`; `PUBLIC=1 ./deploy.sh` opts into a public URL (front it with your own authorization first).
- **Concurrency:** `--concurrency` bounds the per-instance request queue behind the engine lock; `--max-instances` controls fan-out. For read-heavy traffic, more instances beat more threads.
- **Secrets:** use `--set-secrets` with Secret Manager instead of the demo's plain env var.
- **Rebake:** re-run `deploy.sh` after editing `init_db.py` — Cloud Build produces a fresh image.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Build failed because the default service account is missing required IAM permissions` | Projects created after Cloud Build's 2024 service-account change: the compute default SA lacks build permissions | Have a project admin grant it `roles/cloudbuild.builds.builder` — or skip Cloud Build entirely: build anywhere amd64 (even [Cloud Shell](https://cloud.google.com/shell)), push to Artifact Registry, and `gcloud run deploy --image` |
| `PUBLIC=1` deploy fails with an IAM error | Org policy forbids `allUsers` bindings (common on corporate projects) | Use the default private deploy and call with an identity token (`curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" $URL/health`) — which is also the safer choice given the SSRF note above |
| First request after idle is slow | Scale-from-zero pulls the ~1 GB image | See the cold-start levers above |
| `/ask` → 503 | `ANTHROPIC_API_KEY` not set at deploy time | Redeploy with the env var exported |

## Cost & teardown

Idle cost is near zero: `min-instances 0` stops compute billing, leaving image storage in Artifact Registry (cents). Clean up when done:

```bash
./teardown.sh   # deletes the service and the images Cloud Build pushed for it
```

## Try next

- The other lanes: [AWS Lambda](../aws-lambda/) and [Azure Container Apps](../azure-container-apps/) run the same app — [compare the measured economics](../serverless-analyst/#the-lanes-measured).
- Point `init_db.py` at your own data and give the analyst a schema note in `agent.py`'s system prompt.
- Want the analyst to keep its tables and conversation? [Where stateful lives in this series](../serverless-analyst/#where-state-lives) — today's snapshot tiers and the planned `chdb.durable` 2.0.
