# A data analyst agent with chDB in 50 lines — on Azure Container Apps

The Azure lane of the [serverless analyst series](../serverless-analyst/): the same 50-line analyst — Claude plus one `execute_sql` tool plus [chDB](https://github.com/chdb-io/chdb) (in-process ClickHouse) — as a **scale-to-zero container** on [Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/). The app and image are the series' shared ones (`agent.py`, `main.py`, `init_db.py`, `Dockerfile` — [what's shared and what isn't](../serverless-analyst/#the-app-shared-across-every-lane)); the ~60-line `deploy.sh` is the only Azure-specific code, and it never needs Docker on your laptop — **ACR Tasks builds the image server-side**.

## Why Container Apps for this

| chDB property | Azure Container Apps property |
|---|---|
| Engine + data live inside the process | Everything ships in one image — no database to provision |
| The store can be baked at build time | ACR Tasks builds server-side; a cold instance pulls and serves, no data download on boot |
| ~10 ms aggregations over 1M rows in-process | `min-replicas: 0` means idle = free |
| One engine per instance, no shared server | The platform adds replicas under load (`max-replicas`) — each with its own engine |

State is instance-local and ephemeral by design — [where stateful lives in this series](../serverless-analyst/#where-state-lives) (on Azure, the platform snapshot tier is Container Apps Sandboxes; the portable 2.0 is `chdb.durable`).

## Deploy

Prerequisites: `az login` against a subscription where you can create resource groups, a container registry, and Container Apps (the script registers the `Microsoft.App` / `Microsoft.ContainerRegistry` / `Microsoft.OperationalInsights` providers if needed).

```bash
export ANTHROPIC_API_KEY=sk-...   # optional — omit to deploy /query only
./deploy.sh                       # westus by default; REGION=... to override
```

The script creates a Basic registry, has ACR Tasks build the image in the cloud (this is where `init_db.py` downloads and bakes the 1M-row ClickBench store), stands up a Container Apps environment, and rolls out the app with external ingress at 2 vCPU / 4 GiB, `min-replicas 0`. Measured on this exact code (westus):

```
==> registry chdbanalyst<r> + server-side image build (bakes the 1M-row store)
Run cf1 was successful after 1m55s
==> Container Apps environment chdb-analyst-env
==> service https://chdb-analyst.<env-hash>.westus.azurecontainerapps.io
==> first hit (cold: instance start + engine init): 37741 ms
    {"status":"ok","engine":"chdb 4.2.1","baked_rows":1000000,"instance":"87be9faf","uptime_s":26.4}
==> warm hit: 496 ms
```

Ask it something:

```bash
curl -s $URL/ask -H 'Content-Type: application/json' \
  -d '{"question": "Which regions drive the most traffic?"}'
# → "| 1 | 229 | 426,435 | 27,961 | …"  (turns: 2, ~10s — one Claude round-trip + in-process SQL)
```

## The scale-to-zero economics, measured

| Path | Time to first response |
|---|---|
| Warm instance | **~450–600 ms** wire (about 33 ms of it is the engine; the rest is TLS + routing + region RTT) |
| Cold start after scale-to-zero | **30.1 s** — dominated by the image pull; the app itself is serving within ~5 s of the container starting |
| Very first hit after `deploy.sh` | ~38 s — cold start plus the revision still provisioning |

([How the other lanes compare.](../serverless-analyst/#the-lanes-measured)) The levers when 30 s matters: `--min-replicas 1` trades idle compute for zero cold starts, `BAKE_PARTITIONS` shrinks the store and the pull, or keep the store out of the image and query object storage live (`s3()` over parquet).

## Container Apps specifics

- **Ingress (important):** `/query` runs caller-supplied SQL, so `deploy.sh` uses **internal ingress by default** (reachable only inside the Container Apps environment); `PUBLIC=1 ./deploy.sh` switches to external for a throwaway public demo. No managed identity is attached, so there is no cloud credential to reach via SSRF — but front it with Container Apps authentication before exposing anything real.
- **Concurrency:** per-replica concurrency plus `--max-replicas` controls fan-out over the engine lock. For read-heavy traffic, more replicas beat more threads.
- **Secrets:** use `--secrets` + `secretref:` instead of the demo's plain env var.
- **Rebake:** re-run `deploy.sh` after editing `init_db.py` — ACR Tasks produces a fresh image, still with no local Docker.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `MissingSubscriptionRegistration` on first deploy | Provider not registered in this subscription | The script registers `Microsoft.App`/`Microsoft.ContainerRegistry`/`Microsoft.OperationalInsights`; registration takes a minute |
| `az containerapp env create` prints an internal CLI error | Known azure-cli output-formatting bug on some versions | Add `-o none` (the script does); check the environment actually exists with `az containerapp env show` |
| First request after idle takes ~30 s | Scale-from-zero pulls the ~1 GB image | See the cold-start levers above |
| `/ask` → 503 | `ANTHROPIC_API_KEY` not set at deploy time | Re-run `deploy.sh` with the env var exported |
| Registry auth errors on `containerapp create` | Basic ACR without admin user | The script enables `--admin-enabled`; for production prefer a managed identity + `AcrPull` |

## Cost & teardown

Idle cost is near zero by design: `min-replicas 0` stops compute billing, leaving only the Basic registry (~$0.17/day) and the image storage. Everything lives in one resource group:

```bash
./teardown.sh   # deletes the resource group: app, environment, registry, images
```

## Try next

- The other lanes: [AWS Lambda](../aws-lambda/) and [Google Cloud Run](../gcp-cloud-run/) run the same app — [compare the measured economics](../serverless-analyst/#the-lanes-measured).
- Point `init_db.py` at your own data and give the analyst a schema note in `agent.py`'s system prompt.
- Want the analyst to keep its tables and conversation? [Where stateful lives in this series](../serverless-analyst/#where-state-lives) — today's snapshot tiers and the planned `chdb.durable` 2.0.
