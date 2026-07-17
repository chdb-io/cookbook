# chDB on E2B: a stateful analytical database inside your agent's sandbox

**What you'll learn:** run [chDB](https://github.com/chdb-io/chdb) (in-process ClickHouse for Python) inside an [E2B](https://e2b.dev) sandbox, with the data surviving pause/resume.

E2B sandboxes are the agent's working computer: filesystem, shell, Python kernel, sub-second boot — and `pause()`/resume snapshots memory and disk while billing stops. chDB runs inside the sandbox's Python process (no server, no connection string) and stores tables as ClickHouse MergeTree files on the sandbox disk. So the sandbox is the agent's computer and chDB is its analytical memory: data ingested in one session is still queryable after a pause, days later, with nothing external to provision or pay for in between.

```
e2b-sandbox/
├── e2b.Dockerfile   # sandbox template: official code-interpreter image + chdb
├── analyst.py       # runnable walkthrough: create → seed → query → pause → resume → verify
└── teardown.sh      # kill leftover demo sandboxes, delete the template
```

Prereqs: an [E2B account](https://e2b.dev/dashboard) (free tier is fine), then `pip install e2b-code-interpreter` and `export E2B_API_KEY=...`.

## Getting chDB into a sandbox

**(a) Public `chdb` template — coming soon.** Once published, `Sandbox.create("chdb")` will work on any account with no build step. Until then:

**(b) Build the template from `e2b.Dockerfile`** (needs the CLI: `npm i -g @e2b/cli && e2b auth login`):

```bash
e2b template create chdb --dockerfile e2b.Dockerfile \
  -c "sudo --preserve-env=E2B_LOCAL /root/.jupyter/start-up.sh" \
  --ready-cmd "curl -sf http://localhost:49999/health"
```

~1 min first build, ~40s cached. The `-c`/`--ready-cmd` flags are **required**: `run_code()` talks to a Jupyter-backed server on port 49999 that the base image ships but a derived template does not auto-start — a template doesn't inherit its base template's start command. These two flags reproduce the base's own config (from [e2b-dev/code-interpreter](https://github.com/e2b-dev/code-interpreter) `template/template.py`). Also: use `e2b template create`; `e2b template build` is the deprecated v1 command and builds nothing.

**(c) `pip install chdb` at runtime.** E2B-to-PyPI bandwidth is good enough that installing the full wheel takes ~8s — a template just saves those seconds and works with internet disabled:

```python
sbx = Sandbox.create()                                             # base template, ~0.5s
sbx.commands.run("pip install --no-cache-dir chdb", timeout=600)   # ~8s
sbx.run_code("import chdb; print(chdb.query('SELECT 1', 'CSV'))")  # ~0.9s
```

## The stateful walkthrough

`python analyst.py` runs this end to end with timings. Abridged:

```python
from e2b_code_interpreter import Sandbox

sbx = Sandbox.create(template="chdb")

# Seed a MergeTree table via a chdb Session — real ClickHouse data parts
# under /home/user/chdb-data on the sandbox disk.
sbx.run_code("""
from chdb.session import Session
sess = Session("/home/user/chdb-data")
sess.query("CREATE DATABASE IF NOT EXISTS demo")
sess.query('''CREATE TABLE IF NOT EXISTS demo.events
  (id UInt64, event_type LowCardinality(String), ts DateTime)
  ENGINE = MergeTree ORDER BY id''')
sess.query('''INSERT INTO demo.events
  SELECT number, ['view','click','purchase','signup','refund'][number % 5 + 1], now()
  FROM numbers(1000000)''')
print(sess.query("SELECT count() FROM demo.events", "CSV"))
sess.close()
""")

sbx.run_code("""
from chdb.session import Session
sess = Session("/home/user/chdb-data")
print(sess.query("SELECT event_type, count() AS n FROM demo.events GROUP BY event_type ORDER BY n DESC", "Pretty"))
sess.close()
""")

sbx.pause(keep_memory=True)   # snapshot memory + disk; billing stops
sbx.connect()                 # resume

sbx.run_code("""
from chdb.session import Session
sess = Session("/home/user/chdb-data")
print("after resume:", sess.query("SELECT count() FROM demo.events", "CSV"))   # 1000000
sess.query("INSERT INTO demo.events SELECT number + 1000000, 'view', now() FROM numbers(1000)")
print("after append:", sess.query("SELECT count() FROM demo.events", "CSV"))   # 1001000
sess.close()
""")

sbx.kill()
```

Close the `Session` at the end of each `run_code` block and reopen it in the next — one writer per session directory, and reopening is free.

Measured on the free tier, prebuilt template, steady state:

| Step | Time |
|---|---|
| `Sandbox.create("chdb")` | ~1.2s |
| first `run_code` (kernel start + `import chdb`) | ~5s |
| insert 1M rows into MergeTree | ~0.3s |
| subsequent queries | ~0.2s |
| `pause(keep_memory=True)` | ~0.3s |
| resume (`connect()`) | ~0.5s |

Caveat: the very first time a template lands on an execution node, create was ~2s and first `run_code` ~17s — a one-time image pull, not steady state. And the ~5s first `run_code` is kernel cold start, not chDB: with the kernel already warm, the first query took ~0.9s.

## Use it from an LLM agent

chDB 4.2+ ships `chdb.agents.ChDBTool`: engine-enforced read-only mode (default on — the model can't write, whatever SQL it produces), result caps (1000 rows / 1 MB default), and typed error envelopes (`{"ok": false, "error": {"type": "UNKNOWN_TABLE", ...}}`). `tool_specs()` returns Anthropic-format tool definitions (`run_select_query`, `list_tables`, `describe_table`, ...); `call()` dispatches one call.

Runs inside the sandbox (via `run_code(..., envs={"ANTHROPIC_API_KEY": ...})`, after `sbx.commands.run("pip install anthropic")`):

```python
import json, anthropic
from chdb.agents import ChDBTool

tool = ChDBTool("/home/user/chdb-data")
client = anthropic.Anthropic()
messages = [{"role": "user", "content": "Which event type is most common in demo.events, and what share of all events is it?"}]
while True:
    resp = client.messages.create(model="claude-opus-4-8", max_tokens=2048,
                                  tools=tool.tool_specs(), messages=messages)
    if resp.stop_reason != "tool_use":
        print(next(b.text for b in resp.content if b.type == "text"))
        break
    messages.append({"role": "assistant", "content": resp.content})
    messages.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(tool.call(b.name, b.input))}
        for b in resp.content if b.type == "tool_use"]})
tool.close()
```

## Troubleshooting

**`run_code` fails with "port is not open" (port 49999, code 502) on a custom template.** The template was built without a start command (not inherited from the base image's template). Rebuild with the `-c` and `--ready-cmd` flags shown above.

**With `allow_internet_access=False`, `url()`/`s3()` queries hang forever — no error.** E2B's firewall in this mode accepts the TCP connection then black-holes it, so the engine waits on a TLS reply that never comes; in chdb 4.2.x no timeout setting, Python signal, or `run_code(timeout=)` interrupts it (we measured a query hanging for 10 minutes straight until the sandbox itself died, surfacing only as E2B's "sandbox was killed while the request was in progress"). Fix: keep internet on (the default), or upload data into the sandbox (`sbx.files.write(...)` or bake it into the template) and query with `file()`. If a remote-table query hangs in a sandbox, suspect network config first — don't wait for an error. Newer chDB releases add a client-side network deadline that turns this into a typed error; check the [release notes](https://github.com/chdb-io/chdb/releases).

## Costs

The free Hobby tier covers this tutorial: 100 sandbox-hours/month, per-second billing, and paused sandboxes don't bill. `analyst.py` uses a couple of sandbox-minutes per run. Clean up with `./teardown.sh`.

## Try next

- `sbx.files.write()` a Parquet file in and expose it to the model with `ChDBTool.attach_file()`.
- With internet on, `INSERT INTO ... SELECT ... FROM s3(...)` materializes a public dataset locally once; every later question (including post-resume) is answered from disk.
- One sandbox per user — create is ~1s from a warm template, so a private analyst per user is just a dict of sandbox IDs.
- Same pattern, different suspend model: [AWS Lambda MicroVMs](../lambda-microvms/) and the stateless [serverless series](../serverless-analyst/).
