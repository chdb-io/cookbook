# A data analyst agent with chDB — on AWS Lambda

The AWS lane of the [serverless analyst series](../serverless-analyst/): the [`chdb-serverless`](https://pypi.org/project/chdb-serverless/) analyst as a **classic Lambda container function** — per-request billing (idle = exactly zero) and a Function URL. The app is `pip install chdb-serverless`; the [shared image](../serverless-analyst/#the-image) carries the [AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter) (active only under the Lambda runtime, inert elsewhere), which translates invocations into HTTP against the uvicorn app.

## Why Lambda for this

| chDB property | Lambda property |
|---|---|
| Engine + data live inside the process | Everything ships in one image (up to 10 GB) — no database to provision |
| The store is baked at build time | Lambda lazy-loads image chunks on demand — a ~1 GB image cold-starts in ~34 s |
| ~10 ms aggregations over 1M rows in-process | Per-request billing: idle costs exactly zero |
| One engine per instance | Each concurrent request gets its own sandbox — isolation is the default |

## Deploy

The hardened AWS deploy/teardown scripts live with the package:

```bash
git clone https://github.com/chdb-io/chdb-lambda && cd chdb-lambda
export ANTHROPIC_API_KEY=sk-...    # optional — omit for /query only
deploy/aws-lambda/deploy.sh        # us-west-2 by default; REGION=... to override
```

The script builds the [shared image](../serverless-analyst/Dockerfile) (which `pip install`s the published package and bakes the 1M-row store), pushes it to ECR, and creates a 4 GB x86_64 function with a Function URL. Measured on the published package: **~34 s cold start**, **~500 ms warm**, `/query` ~255 ms.

## AWS specifics (handled by the deploy script)

- **Read-only filesystem** except `/tmp`: the function's `ImageConfig.Command` copies the baked store to `/tmp` and sets `CHDB_STORE=local:/tmp/chdb-data` at boot, so the image stays shared.
- **Container packaging** is the route: chDB doesn't fit Lambda's 250 MB zip limit (so SnapStart, zip-only, is out).
- **Auth**: the Function URL defaults to `AWS_IAM` (SigV4-signed calls); `PUBLIC=1` opts into an unauthenticated URL — `/query` runs arbitrary SQL, so keep it private unless you front it with your own auth.

## Try next

- The same package on [Google Cloud Run](../gcp-cloud-run/) and [Azure Container Apps](../azure-container-apps/) — [compare the measured economics](../serverless-analyst/#the-lanes-measured).
- Keep the analyst's tables and conversation across invocations: [Lambda MicroVMs](../lambda-microvms/) runs this exact app with platform snapshots today; portable S3-backed state (`chdb-serverless[durable]`) is the planned 2.0.
