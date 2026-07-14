# A data analyst agent with chDB in 50 lines — on AWS Lambda

The AWS lane of the [serverless analyst series](../serverless-analyst/): the same 50-line analyst — Claude plus one `execute_sql` tool plus [chDB](https://github.com/chdb-io/chdb) (in-process ClickHouse) — as a **classic Lambda container function** with per-request billing and a public Function URL. The app is the series' shared one (`agent.py`, `main.py`, `init_db.py` — [what's shared and what isn't](../serverless-analyst/#the-app-shared-across-every-lane)); this lane's `Dockerfile` adds exactly **one line** to the shared image — the [AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter), which translates Lambda invocations into HTTP against the same uvicorn app and is inert everywhere else.

## Why Lambda for this

| chDB property | Lambda property |
|---|---|
| Engine + data live inside the process | Everything ships in one image (up to 10 GB) — no database to provision |
| The store can be baked at build time | Lambda lazy-loads image chunks on demand — our ~1 GB image cold-starts in ~10 s |
| ~10 ms aggregations over 1M rows in-process | Per-request billing: idle costs exactly zero |
| One engine per instance, no shared server | Each concurrent request gets its own sandbox — isolation is the default |

State is instance-local and ephemeral by design — [where stateful lives in this series](../serverless-analyst/#where-state-lives) (on AWS, the platform snapshot tier is [Lambda MicroVMs](../lambda-microvms/), which runs this exact app with suspend/resume; the portable 2.0 is `chdb.durable`).

## The two Lambda realities

The deploy script handles two things the other lanes don't have:

- **The container filesystem is read-only** except `/tmp`. chDB needs a writable data directory (status file, locks), so the function's `ImageConfig.Command` copies the baked store to `/tmp` at boot and points `CHDB_DATA_PATH` there — a configuration override, so the image itself stays shared. `--ephemeral-storage 1024` gives the copy headroom.
- **Container packaging is the route.** Lambda's zip packaging caps at 250 MB unzipped, which chDB does not fit; container images (up to 10 GB) do. A consequence: **SnapStart is not supported** — it applies only to zip-packaged functions.

## Deploy

Prerequisites: AWS CLI v2 with credentials that can manage ECR, IAM roles, and Lambda; docker or podman with an amd64 builder.

```bash
export ANTHROPIC_API_KEY=sk-...   # optional — omit to deploy /query only
./deploy.sh                       # us-west-2 by default; REGION=... to override
```

The script creates an ECR repository, builds and pushes the image (this is where `init_db.py` bakes the store), creates a minimal execution role, and rolls out a 4 GB x86_64 function with a public Function URL. Measured on this exact code (us-west-2):

```
==> function URL https://<id>.lambda-url.us-west-2.on.aws
==> first hit (cold): 9934 ms
    {"status":"ok","engine":"chdb 4.2.1","baked_rows":1000000,"instance":"fd9b8f48","uptime_s":0.1}
==> warm hit: 524 ms
```

Ask it something:

```bash
curl -s $URL/ask -H 'Content-Type: application/json' \
  -d '{"question": "Which regions drive the most traffic?"}'
# → "| 1 | 229 | 426,435 | 27,961 | …"  (turns: 2, ~10s — one Claude round-trip + in-process SQL)
```

## The serverless economics, measured

| Path | Time to first response |
|---|---|
| Warm sandbox | **~500–550 ms** wire |
| Cold start | **9.9 s** — sandbox init + `/tmp` store copy + engine init; Lambda's on-demand image chunk loading keeps this well under a full ~1 GB pull |

The fastest cold start of the three lanes — [see the comparison](../serverless-analyst/#the-lanes-measured). The levers when 10 s still matters: provisioned concurrency (paid-warm), a smaller `BAKE_PARTITIONS`, or querying object storage live instead of baking.

## Lambda specifics

- **Concurrency:** every concurrent request gets its own sandbox, so there is no engine-lock contention across requests — scale-out is the platform default.
- **Auth:** the demo Function URL is public (`--auth-type NONE`); switch to `AWS_IAM` and SigV4-sign requests for anything real.
- **S3 access:** `s3()` table-function reads are IAM-signed via the execution role — extend the role's policy for your buckets.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 502 + adapter logs `app is not ready` forever | chDB crashed on the read-only filesystem (`Cannot open file .../status: Read-only file system`) | Keep the `ImageConfig.Command` override that copies the store to `/tmp` (deploy.sh does) |
| First hit after deploy is slow, then fast | Cold start: image chunks + `/tmp` copy + engine init | Provisioned concurrency, or accept ~10 s on first touch |
| `/ask` → 503 | `ANTHROPIC_API_KEY` not set at deploy time | Re-run `deploy.sh` with the env var exported |

## Cost & teardown

Idle cost is exactly zero for compute (per-request billing); ECR storage for the ~1 GB image runs pennies per month. Clean up when done:

```bash
./teardown.sh   # deletes the function, its URL, the role, and the ECR repository
```

## Try next

- The other lanes: [Google Cloud Run](../gcp-cloud-run/) and [Azure Container Apps](../azure-container-apps/) run the same app — [compare the measured economics](../serverless-analyst/#the-lanes-measured).
- Point `init_db.py` at your own data and give the analyst a schema note in `agent.py`'s system prompt.
- Want the analyst to keep its tables and conversation? [Lambda MicroVMs](../lambda-microvms/) runs this exact app with platform snapshots today, and portable S3-backed state (`chdb.durable`) is the planned 2.0 — [the tier ladder](../serverless-analyst/#where-state-lives).
