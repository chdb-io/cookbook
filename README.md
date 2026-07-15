# chDB Cookbook

Runnable notebooks and code samples for [chDB](https://github.com/chdb-io/chdb) — the in-process OLAP SQL engine powered by ClickHouse.

This cookbook mirrors the structure of the [Anthropic](https://github.com/anthropics/anthropic-cookbook) and [OpenAI](https://github.com/openai/openai-cookbook) cookbooks: short, focused, runnable examples that show one technique each. Notebooks are organized by goal — query data, build an agent, run a benchmark — not by API surface.

> Status: pre-launch placeholder. Initial notebooks will be added incrementally. Contributions welcome — see [Contributing](#contributing).

## Getting started

```bash
pip install chdb jupyter
git clone https://github.com/chdb-io/cookbook
cd cookbook
jupyter lab
```

Every notebook runs end-to-end with `pip install chdb` and the dependencies listed in its first cell — no external services required (except where the notebook explicitly federates to ClickHouse Cloud).

## Conventions

- Notebooks are self-contained — first cell installs everything, last cell prints expected output.
- All datasets are public (S3 open buckets, Hugging Face, or pip-installed fixtures).
- Each notebook has a 1-paragraph "What you'll learn" header and a "Try next" footer.
- File names use kebab-case; categories use lowercase singular nouns.

## Cookbooks

### agents
- [Federated SQL for Claude Dynamic Workflows](dynamic-workflows/README.md) — give every subagent an in-process engine that joins S3, Postgres, ClickHouse, an HTTP API, and a DataFrame in one query.
- [A data analyst agent with chDB in 50 lines — on AWS Lambda MicroVMs](lambda-microvms/README.md) — build a complete analyst agent (Claude + one `execute_sql` tool + chDB), then give every user their own Firecracker-isolated copy: snapshot-hot starts, suspend/resume with memory intact, one MicroVM per session. *(Deployment recipe; the agent also runs standalone on a laptop.)*
- [One analyst, three clouds — chDB on serverless](serverless-analyst/README.md) — the series hub: the `chdb-serverless` package (one `pip install`) deployed as one image to AWS Lambda, Cloud Run, and Azure Container Apps, cold-start economics measured side by side, and where stateful lives.
- [A data analyst agent with chDB — on Google Cloud Run](gcp-cloud-run/README.md) — the `chdb-serverless` analyst as a scale-to-zero Cloud Run container: idle = free, private by default, cold-start economics measured. *(App is `pip install chdb-serverless`.)*
- [A data analyst agent with chDB — on Azure Container Apps](azure-container-apps/README.md) — the `chdb-serverless` analyst as a scale-to-zero Container Apps deployment: server-side ACR build, internal ingress by default, cold-start economics measured. *(App is `pip install chdb-serverless`.)*
- [A data analyst agent with chDB — on AWS Lambda](aws-lambda/README.md) — the `chdb-serverless` analyst as a classic Lambda container function: per-request billing, a Function URL (IAM-auth by default). *(App is `pip install chdb-serverless`; the image carries the Lambda Web Adapter.)*

### ingestion
- [OTEL ingestion buffer in Node.js](otel-ingestion-buffer/README.md) — use chDB as an off-heap ingestion buffer in a Node.js service: zero-copy span ingestion (no `JSON.parse` on the main thread), engine-side enrichment, and native-protocol export — with flow control and failure/retry recipes. *(Node.js, not a Python notebook.)*

## Contributing

PRs welcome. To propose a notebook:

1. Open an issue with the working title and a one-paragraph summary of what it teaches.
2. Place the notebook under the matching category directory.
3. Ensure it runs top-to-bottom on a clean `pip install chdb` environment.
4. Add a one-line entry to this README under the right category.

For larger contributions (new categories, multi-notebook tutorials), please discuss in [Discord](https://discord.gg/D2Daa2fM5K) first.

## License

Apache 2.0 — see [LICENSE](LICENSE).

## Related

- Main chDB repository: https://github.com/chdb-io/chdb
- chDB documentation: https://clickhouse.com/docs/chdb
- LLM-friendly index: https://clickhouse.com/docs/chdb/llms.txt
- Awesome chDB: https://github.com/chdb-io/awesome-chdb
- Community: https://discord.gg/D2Daa2fM5K
