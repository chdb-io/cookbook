"""Generate test data for the v5 benchmarks (JSON + vector)."""

import json
import os
import random

import pandas as pd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
random.seed(42)
np.random.seed(42)

# ---------- 1M synthetic agent-event JSON ----------
N_JSON = 1_000_000
TOOLS = ["search", "calculator", "code_exec", "browse", "rag", "memory_get",
         "memory_set", "file_read", "shell", "sql"]
TIERS = ["free", "basic", "premium"]
REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"]
STATUSES = ["ok", "ok", "ok", "ok", "ok", "ok", "ok", "error", "timeout", "ok"]

def make_event(i):
    user_id = random.randint(1, 50_000)
    return {
        "id": i,
        "tool": random.choice(TOOLS),
        "user": {
            "id": user_id,
            "tier": TIERS[user_id % len(TIERS)],
            "region": random.choice(REGIONS),
        },
        "request": {
            "query_length": random.randint(5, 500),
            "filters_count": random.randint(0, 5),
        },
        "response": {
            "status": random.choice(STATUSES),
            "latency_ms": round(random.lognormvariate(3.5, 1.0), 2),
            "results": random.randint(0, 50),
        },
    }

print(f"Generating {N_JSON:,} synthetic JSON events...")
rows = [{"id": e["id"], "payload": json.dumps(make_event(i))} for i, e in enumerate(make_event(i) for i in range(N_JSON))]
df_json = pd.DataFrame(rows)
out = os.path.join(HERE, "agent_events.parquet")
df_json.to_parquet(out, compression="zstd")
print(f"  wrote {out} ({os.path.getsize(out)/1024/1024:.0f} MB, {N_JSON:,} rows)")

# ---------- 100K random 384-d Float32 embedding vectors ----------
N_VEC = 100_000
DIM = 384
print(f"\nGenerating {N_VEC:,} random {DIM}-d float32 embedding vectors...")
vecs = np.random.randn(N_VEC, DIM).astype(np.float32)
vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)  # unit-normalised
arr = pa.array([row.tolist() for row in vecs], type=pa.list_(pa.float32(), DIM))
ids = pa.array(range(N_VEC), type=pa.int64())
table = pa.Table.from_arrays([ids, arr], names=["id", "emb"])
out_vec = os.path.join(HERE, "embeddings.parquet")
pq.write_table(table, out_vec, compression="zstd")
print(f"  wrote {out_vec} ({os.path.getsize(out_vec)/1024/1024:.0f} MB, {N_VEC:,} rows × {DIM} dims)")

# A query vector — first row, normalised
query_vec = vecs[0].tolist()
import json as _json
with open(os.path.join(HERE, "query_vec.json"), "w") as f:
    _json.dump(query_vec, f)
print(f"  saved query_vec.json (using row[0] as the probe)")
