# A data analyst agent with chDB in 50 lines — on AWS Lambda MicroVMs

**What you'll learn:** how to build a complete data-analyst agent in ~50 lines of Python — Claude plus one `execute_sql` tool plus [chDB](https://github.com/chdb-io/chdb) (in-process ClickHouse) — and then give every user their own private copy of it on [AWS Lambda MicroVMs](https://aws.amazon.com/lambda/lambda-microvms/): Firecracker-isolated, hot from the first millisecond thanks to a snapshot taken *after* the engine is warm, suspended for free when idle, and resumed with the analyst's memory intact. The same skeleton (an app plus six lifecycle hooks in one process, a Dockerfile that bakes data at build time, and a deploy script of plain `aws` CLI calls) carries any chDB workload onto MicroVMs — swap the dataset and the endpoints for your own.

> *ClickHouse Cloud is where your data lives. chDB is what your agent thinks with. Lambda MicroVMs is where it gets to think — in private.*

chDB is a named launch partner for AWS Lambda MicroVMs. For a full reference architecture on this combination (Bedrock agent, five-cloud federation, chat UI, CDK), see [nklmish/chdb-lambda-microvm-demo](https://github.com/nklmish/chdb-lambda-microvm-demo). This cookbook is the opposite end of the spectrum: the minimal version you can type yourself.

## Part 1 — the analyst, on your laptop

The whole "database tier" of this agent is `pip install chdb`. The engine runs inside the Python process, so the agent needs exactly one tool:

```python
TOOLS = [{
    "name": "execute_sql",
    "description": "Run one ClickHouse SQL statement on the in-process engine; returns JSON.",
    "input_schema": {"type": "object",
                     "properties": {"sql": {"type": "string"}},
                     "required": ["sql"]},
}]
```

[`agent.py`](agent.py) is the complete agent — a classic tool-use loop, 47 lines of code. Try it locally:

```bash
pip install chdb anthropic
python init_db.py                       # bakes 1M rows of ClickBench web analytics (~18s, 122 MiB)
CHDB_DATA_PATH=/app/chdb-data ANTHROPIC_API_KEY=sk-... python agent.py
```

```
chDB analyst ready — ask about demo.hits (Ctrl-D to exit)
Which regions drive the most traffic, and how mobile are they?

| Region | Hits | Unique Users | Mobile % |
|--------|-----:|-------------:|---------:|
| **229** | 426,435 | 27,961 | 3.5% |
| **2** | 148,193 | 10,413 | 3.7% |
| **208** | 30,614 | 3,073 | **17.2%** |
...
**Traffic is extremely concentrated.** Region 229 alone accounts for ~43% of the
top-20 traffic ... **Region 208 is a major outlier at 17.2% mobile** — nearly 5×
the average and worth investigating ...

Want me to dig into what makes Region 208 so mobile-heavy?
```

Ask the follow-up ("yes — what's its OS mix?") and the agent remembers which region you meant: the conversation and every SQL result live in its process. Keep that in mind for Part 2 — *this* is the state a MicroVM suspends and resumes.

The agent writes ClickHouse SQL, executes it in-process (no connection pool, no server, ~10 ms for a GROUP BY over 1M rows), reads the result, and answers. Because chDB speaks ClickHouse SQL, the same tool also reaches *external* data — `s3()`, `postgresql()`, `mysql()`, `remoteSecure()` to ClickHouse Cloud — and can materialize what it learns into local MergeTree tables.

## Part 2 — one private analyst per user, on Lambda MicroVMs

An analyst that holds state (materialized tables, conversation memory) shouldn't be shared between users. Lambda MicroVMs solves exactly this: each MicroVM is a Firecracker-isolated VM with its own kernel, memory, and disk, launched from a snapshot in milliseconds, with a dedicated HTTPS endpoint, auto-suspended when idle (no compute charge) and resumed with **memory and disk state intact** for up to 8 hours.

That maps one-to-one onto what an embedded engine wants:

| chDB property | MicroVM property |
|---|---|
| Engine + data live inside the process | Snapshot captures the warm process → first query is hot |
| Session state is MergeTree on local disk | VM disk survives suspend/resume |
| Conversation memory is process RAM | VM RAM survives suspend/resume |
| Pushdown: reads S3 directly, returns small answers | Only the answer crosses the dedicated endpoint |
| One engine per user = no shared server to overload | One MicroVM per user = hardware isolation |

### The files

```
lambda-microvms/
├── agent.py       # Part 1 — the 50-line analyst, unchanged
├── main.py        # one process, two ports: the app (:8080) + 6 lifecycle hooks (:9000)
├── init_db.py     # build time: bake ClickBench partitions into MergeTree
├── Dockerfile     # two-stage; snapshot-friendly
├── deploy.sh      # plain aws CLI: bucket → roles → image → run → smoke test
└── teardown.sh    # remove everything deploy.sh created
```

The one design decision worth copying is in [`main.py`](main.py): the app and the lifecycle hooks run as **two servers in one process**. Lambda builds your image by running the container, polling the `/ready` hook, and snapshotting the VM when it returns 200. Because our `/ready` warms the chDB store *in the same process the app serves from*, the snapshot contains a hot engine — every MicroVM launched from it answers its first query with zero initialization.

```
build time                                    run time (per user session)
──────────                                    ───────────────────────────
docker build                                  run-microvm  ──▶ RUNNING (ms, from snapshot)
  └─ init_db.py bakes 1M rows                   │  /query, /ask over dedicated HTTPS endpoint
container starts (app + hooks)                  │  idle 15 min ──▶ SUSPENDED (no compute charge)
  └─ POST /ready → warms chDB → 200             │  traffic ──▶ RUNNING (RAM + disk intact)
Lambda snapshots the warm VM ──▶ image          └─ terminate (or 8h max) ──▶ gone
```

The other five hooks are one-liners: `/validate` runs a sample query so the platform can profile which snapshot pages to prefetch, `/run` and `/resume` reseed randomness (a snapshot freezes RNG state — every VM launched from it would otherwise generate identical "random" numbers), `/suspend` and `/terminate` just log (MergeTree writes are already durable on the VM disk).

### Deploy

Prerequisites: AWS CLI ≥ 2.35.12 (`aws lambda-microvms help` should work), credentials with `lambda-microvms:*`, IAM role management, and S3 permissions, plus `lambda:PassNetworkConnector` on the AWS-managed default connectors.

```bash
export ANTHROPIC_API_KEY=sk-...   # optional — omit to deploy /query only
./deploy.sh                       # us-west-2 by default; REGION=... to override
```

The script creates a private artifact bucket, two scoped IAM roles, a MicroVM image (this is where your Dockerfile runs and the dataset gets baked — the build takes a few minutes), then launches one MicroVM and smoke-tests it:

```
==> account 123456789012, region us-west-2
==> bucket s3://chdb-sql-sandbox-artifacts-123456789012-us-west-2
==> roles chdb-sql-sandbox-build-role, chdb-sql-sandbox-exec-role
==> uploaded s3://chdb-sql-sandbox-artifacts-123456789012-us-west-2/app.zip
==> base image arn:aws:lambda:us-west-2:aws:microvm-image:al2023-1
==> building image (bakes the dataset, warms chDB, snapshots) .............. done (version 1.0)
==> microvm microvm-b913e662-4da5-39ac-a220-a3ceb859eab8
==> endpoint 2ccf9284-65fd-9f53-9f1f-2d5cd53f0779.lambda-microvm.us-west-2.on.aws
==> waiting for MicroVM . RUNNING
==> smoke test: GET /health
{"status": "ok", "engine": "chdb 4.2.0", "baked_rows": 1000000, "boot_id": "761cd0fa…", "uptime_s": 17.9}
```

(Measured on this exact code: the image build — pip install, 1M-row bake, warm, snapshot — took 3m36s; a MicroVM launched from it reaches `RUNNING` in about 4 seconds.)

### Talk to your private engine

Every request needs the auth token in `X-aws-proxy-auth` (deploy.sh prints one; mint more with `create-microvm-auth-token`).

**Raw SQL against the baked table** — served warm from the snapshot:

```bash
curl -s "https://$ENDPOINT/query" -H "X-aws-proxy-auth: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"sql": "SELECT RegionID, count() AS hits, uniq(UserID) AS users FROM demo.hits GROUP BY RegionID ORDER BY hits DESC LIMIT 5"}'
```

```json
{"elapsed_ms": 12.0, "result": {"data": [[229,426435,27961],[2,148193,10413],[208,30614,3073],[1,28577,1720],[34,14329,1428]],
 "rows": 5, "statistics": {"rows_read": 1000000, "bytes_read": 13000000}}}
```

12 ms for a GROUP BY over 1M rows, over the public internet, on a VM that has existed for seconds. There was no engine to start and no data to load — the snapshot already contained both. (The very first scan on a fresh VM pays lazy page-in from the snapshot, ~1.7 s on this dataset; everything after runs at the numbers above.)

**One SQL statement, local + live S3** — the engine federates; only the aggregate crosses the endpoint:

```bash
curl -s "https://$ENDPOINT/query" -H "X-aws-proxy-auth: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"sql": "SELECT src, count() AS events, uniq(UserID) AS users FROM (SELECT '\''baked-local'\'' AS src, UserID FROM demo.hits UNION ALL SELECT '\''s3-live'\'' AS src, UserID FROM s3('\''https://clickhouse-public-datasets.s3.amazonaws.com/hits_compatible/athena_partitioned/hits_1.parquet'\'', NOSIGN)) GROUP BY src ORDER BY src"}'
```

```json
{"elapsed_ms": 8829.5, "result": {"data": [["baked-local",1000000,79989],["s3-live",1000000,200570]],
 "rows": 2, "statistics": {"rows_read": 2000000, "bytes_read": 182899180}}}
```

The engine pulled 183 MB from S3 and returned two rows. That's the pushdown argument for putting chDB *inside* the isolated VM: data moves engine-side, answers move client-side.

**Ask the analyst:**

```bash
curl -s "https://$ENDPOINT/ask" -H "X-aws-proxy-auth: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question": "Which regions drive the most traffic, and how mobile are they?"}'
```

```json
{"answer": "Here's the traffic breakdown by region, ranked by hits, with their mobile usage rates:\n\n| Rank | RegionID | Hits | Unique Users | Mobile % | ...\n| 3 | **208** | 30,614 | 3,073 | **17.2%** ⬅ | ...",
 "turns": 4, "elapsed_ms": 15256.6}
```

The agent wrote the SQL, ran it in-process, and spotted the region-208 mobile outlier on its own — from inside the MicroVM.

### Suspend, resume, and the analyst's memory

Have the agent (or `/query`) materialize something, then bounce the VM:

```bash
curl -s "https://$ENDPOINT/query" -H "X-aws-proxy-auth: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"sql": "CREATE TABLE demo.session_cache ENGINE = MergeTree ORDER BY tuple() AS SELECT URL, count() AS c FROM demo.hits GROUP BY URL ORDER BY c DESC LIMIT 100"}'

aws lambda-microvms suspend-microvm --microvm-identifier $MICROVM_ID --region us-west-2
aws lambda-microvms resume-microvm  --microvm-identifier $MICROVM_ID --region us-west-2

curl -s "https://$ENDPOINT/query" -H "X-aws-proxy-auth: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"sql": "SELECT count(), max(c) FROM demo.session_cache", "format": "TabSeparated"}'
```

```
# before suspend:            100    58976
# suspend … state SUSPENDED
# resume  … state RUNNING
# after resume:              100    58976        ← MergeTree intact
# /health after resume: {"status": "ok", ..., "boot_id": "b165473e…"}   ← fresh boot_id: the /resume hook fired and reseeded
```

Both layers of the analyst's memory survive: the MergeTree tables (VM disk) and the conversation history in `main.py`'s process RAM. Verified on this deployment: after `suspend-microvm` + `resume-microvm`, asking `/ask` *"for that mobile-heavy outlier region you flagged, what's its OS mix?"* — no region number given — the analyst answered about region 208, because the whole conversation rode through the suspend inside the VM's RAM. While suspended you pay no compute.

### A second tenant is one command

```bash
aws lambda-microvms run-microvm --image-identifier $IMAGE_ARN --image-version 1.0 ... --region us-west-2
```

Same image, new VM: its own kernel, its own chDB, its own endpoint and tokens. Nothing shared with the first tenant except the immutable snapshot. Measured: the second VM reached `RUNNING` **4 seconds** after `run-microvm`, served the same 1M baked rows under a fresh `boot_id` — and tenant 1's session table simply doesn't exist there:

```json
// on tenant 2, the query tenant 1 materialized:
{"error": "Code: 60. DB::Exception: Unknown table expression identifier 'demo.session_cache' ... (UNKNOWN_TABLE)"}
```

## Adapt it to your workload

- **Your data:** edit the `CREATE TABLE ... AS SELECT` in [`init_db.py`](init_db.py) — anything chDB can read works (S3/HTTP parquet & 80+ formats, `postgresql()`, `mysql()`, `iceberg()`, `deltaLake()`, local files shipped in the zip). Keep the baked store to what the snapshot should carry; reach for everything else live via table functions.
- **Your API:** replace the endpoints in [`main.py`](main.py). Keep the two-servers-one-process shape and make `/ready` exercise whatever your app needs warm — that's what the snapshot captures.
- **Your agent:** swap `demo.hits` and the schema note in `agent.py`'s system prompt. Or delete the agent layer and keep a plain SQL-over-HTTPS sandbox.
- **CI runners / disposable sandboxes:** the same image works run-once — `run-microvm`, hit the endpoint, `terminate-microvm`. A clean, isolated chDB per test run with no shared staging server.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `aws: Invalid choice: lambda-microvms` | CLI older than 2.35.12 | upgrade the AWS CLI |
| `RunMicrovm AccessDenied: lambda:PassNetworkConnector` | caller lacks it on the default connectors | grant it on `arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:*` |
| image build `FAILED` | Dockerfile / bake error | `list-microvm-image-builds` carries `stateReason`; full logs in CloudWatch `/aws/lambda-microvms/*` |
| proxy 502 while `RUNNING` | app not yet listening on 8080 | retry — the deploy script's smoke test already waits |
| `/ask` → 503 | image built without `ANTHROPIC_API_KEY` | re-run `deploy.sh` with the env var set (a new image version is created) |

## Cost & teardown

You pay for MicroVM runtime (suspended = free), image-version storage, the S3 artifact (KBs), and CloudWatch logs. An idle suspended demo costs almost nothing, but clean up when done — image versions incur storage even with nothing running:

```bash
./teardown.sh   # terminates MicroVMs, deletes the image, bucket, and roles
```

## Try next

- Bake more partitions: `BAKE_PARTITIONS=10` as a Docker build arg → 10M rows in the snapshot.
- Point the federation demo at your own bucket, or join `postgresql()` dimension tables in the same statement.
- Wire `/ask` into a real UI and one MicroVM per signed-in user — the [launch-partner demo](https://github.com/nklmish/chdb-lambda-microvm-demo) shows the full production shape.
