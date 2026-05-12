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
