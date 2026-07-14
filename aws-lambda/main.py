"""chDB as a serverless container — an analytical SQL endpoint that scales to zero.

The same SQL-over-HTTP app as the Lambda MicroVMs recipe (/health /query
/ask), packaged as a plain serverless container: the dataset is baked into
the image at build time, every instance boots identical, and the platform
starts and stops instances with demand. The same file deploys unchanged to
Google Cloud Run and Azure Container Apps.

State is instance-local and ephemeral by design — materialized tables and
conversation history live only as long as the instance does. That is the
right contract for a stateless SQL endpoint; durable per-user state is a
separate concern (see the README's 2.0 note).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

import chdb
from chdb import session as chdb_session

import agent

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("chdb-analyst")

DATA_PATH = os.getenv("CHDB_DATA_PATH", "/app/chdb-data")
APP_PORT = int(os.getenv("PORT", "8080"))  # the platform injects PORT

# One embedded engine per instance, one session against the baked store.
# chDB sessions are not thread-safe; FastAPI sync endpoints run in a thread
# pool, so serialize engine access with a lock. (The platform sends an instance
# concurrent requests — the lock is what makes that safe; tune concurrency
# to bound the queue.)
_session = chdb_session.Session(DATA_PATH)
_session_lock = threading.Lock()
_ask_lock = threading.Lock()  # serializes whole analyst turns against _history
_instance_id = uuid.uuid4().hex[:8]
_started_at = time.monotonic()

_JSON_FORMATS = {"JSON", "JSONCompact", "JSONColumns", "JSONObjectEachRow"}


def run_sql(sql: str, fmt: str = "JSONCompact") -> str:
    with _session_lock:
        return _session.query(sql, fmt).data()


app = FastAPI(title="chDB analyst (serverless container)")


class QueryRequest(BaseModel):
    sql: str
    format: str = "JSONCompact"  # any ClickHouse output format


class AskRequest(BaseModel):
    question: str


@app.get("/health")
def health() -> dict:
    rows = run_sql("SELECT count() FROM demo.hits", "TabSeparated").strip()
    return {
        "status": "ok",
        "engine": f"chdb {chdb.__version__}",
        "baked_rows": int(rows),
        "instance": _instance_id,
        "uptime_s": round(time.monotonic() - _started_at, 1),
    }


# Conversation history is per-instance RAM: it survives between requests that
# land on the same warm instance and vanishes on scale-to-zero. Good enough
# for a demo conversation; durable memory is the series' 2.0.
_history: list = []


@app.post("/ask")
def ask(req: AskRequest):
    if not os.getenv("ANTHROPIC_API_KEY"):
        return JSONResponse(
            {"error": "ANTHROPIC_API_KEY not set; /ask is disabled (use /query for raw SQL)"},
            status_code=503,
        )
    started = time.perf_counter()
    # One conversation per instance: serialize whole turns so concurrent
    # requests can't interleave into the shared history, and roll a partial
    # turn back if the model call fails mid-flight.
    with _ask_lock:
        checkpoint = len(_history)
        try:
            answer = agent.ask(req.question, _history, lambda sql: run_sql(sql) or "{}")
        except Exception as exc:
            del _history[checkpoint:]
            return JSONResponse({"error": str(exc)}, status_code=502)
    return {
        "answer": answer,
        "turns": len(_history),
        "instance": _instance_id,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


@app.post("/query")
def query(req: QueryRequest):
    started = time.perf_counter()
    try:
        raw = run_sql(req.sql, req.format)
    except Exception as exc:  # chDB raises RuntimeError with the CH error text
        return JSONResponse({"error": str(exc)}, status_code=400)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if req.format in _JSON_FORMATS:
        body = json.loads(raw) if raw else {}
        return {"elapsed_ms": elapsed_ms, "result": body}
    return PlainTextResponse(raw, headers={"X-Elapsed-Ms": str(elapsed_ms)})


if __name__ == "__main__":
    logger.info("app on :%d, store at %s, instance %s", APP_PORT, DATA_PATH, _instance_id)
    uvicorn.run(app, host="0.0.0.0", port=APP_PORT, log_level="info")
