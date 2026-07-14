# A data analyst agent with chDB — on Azure Container Apps

The Azure lane of the [serverless analyst series](../serverless-analyst/): the [`chdb-serverless`](https://pypi.org/project/chdb-serverless/) analyst as a **scale-to-zero container** on [Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/). The app is `pip install chdb-serverless`; the [shared image](../serverless-analyst/#the-image) is built server-side by ACR Tasks — no local Docker needed.

## Why Container Apps for this

| chDB property | Azure Container Apps property |
|---|---|
| Engine + data live inside the process | Everything ships in one image — no database to provision |
| The store is baked at build time | ACR Tasks builds server-side; a cold instance pulls and serves |
| ~10 ms aggregations over 1M rows in-process | `min-replicas: 0` means idle = free |
| One engine per instance | The platform adds replicas under load — each with its own engine |

## Deploy

The hardened Container Apps deploy/teardown scripts live with the package:

```bash
git clone https://github.com/chdb-io/chdb-lambda && cd chdb-lambda
export ANTHROPIC_API_KEY=sk-...    # optional — omit for /query only
deploy/azure-container-apps/deploy.sh   # westus by default; REGION=... to override
```

Measured on the published package: server-side image build ~2 min, **~20 s cold start** (scale-from-zero), **~450–600 ms warm** (~33 ms of it engine), `/query` ~42 ms.

## Container Apps specifics (handled by the deploy script)

- **Ingress**: deploys with **internal ingress by default** (reachable only inside the environment); `PUBLIC=1` switches to external for a throwaway demo. `/query` runs arbitrary SQL, so internal is the safe default.
- **Registry pull**: uses the app's system-assigned managed identity (`--registry-identity system`) — no ACR password ever lands in a command line.
- **Ownership-safe teardown**: every resource is tagged `chdb-cookbook`, so teardown removes only what deploy created, even inside a pre-existing resource group.

## Try next

- The same package on [AWS Lambda](../aws-lambda/) and [Google Cloud Run](../gcp-cloud-run/) — [compare the measured economics](../serverless-analyst/#the-lanes-measured).
- Stateful per-user analysts — state as a durable object on S3, portable across clouds — are the planned **2.0** (`chdb-serverless[durable]`).
