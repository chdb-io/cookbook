"""chDB on AWS Lambda MicroVMs — a private SQL engine per session.

One Python process serves two HTTP ports:

  :8080  the application — a small SQL-over-HTTP API backed by an embedded
         chDB (ClickHouse) engine with a persistent on-disk store. This is
         the port the MicroVM proxy routes client traffic to.
  :9000  the six Lambda MicroVMs lifecycle hooks, under the platform path
         prefix /aws/lambda-microvms/runtime/v1.

The single-process design is deliberate: the /ready hook warms the chDB
store *in this process* before the platform snapshots the VM, so the
snapshot captures a hot engine. Every MicroVM launched from the image
answers its first query warm — no engine init, no store load.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
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
logger = logging.getLogger("sql-sandbox")

DATA_PATH = os.getenv("CHDB_DATA_PATH", "/app/chdb-data")
APP_PORT = int(os.getenv("PORT", "8080"))
HOOKS_PORT = int(os.getenv("MICROVM_HOOKS_PORT", "9000"))
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"

# One embedded engine per MicroVM, one session against the baked store.
# chDB sessions are not thread-safe; FastAPI sync endpoints run in a thread
# pool, so serialize engine access with a lock.
_session = chdb_session.Session(DATA_PATH)
_session_lock = threading.Lock()
_boot_id = uuid.uuid4().hex
_started_at = time.monotonic()

# Formats whose output is JSON text we can embed directly in the response.
_JSON_FORMATS = {"JSON", "JSONCompact", "JSONColumns", "JSONObjectEachRow"}


def run_sql(sql: str, fmt: str = "JSONCompact") -> str:
    with _session_lock:
        return _session.query(sql, fmt).data()


# ---------------------------------------------------------------------------
# Application (:8080) — what clients reach through the MicroVM endpoint
# ---------------------------------------------------------------------------

app = FastAPI(title="chDB SQL sandbox")


class QueryRequest(BaseModel):
    sql: str
    format: str = "JSONCompact"  # any ClickHouse output format


@app.get("/health")
def health() -> dict:
    rows = run_sql("SELECT count() FROM demo.hits", "TabSeparated").strip()
    return {
        "status": "ok",
        "engine": f"chdb {chdb.__version__}",
        "baked_rows": int(rows),
        "boot_id": _boot_id,
        "uptime_s": round(time.monotonic() - _started_at, 1),
    }


# The conversation lives in this process. That is the point: one MicroVM per
# user session, so suspend/resume preserves the analyst's memory (this list is
# in the VM's RAM snapshot; materialized tables are on its disk).
_history: list = []


class AskRequest(BaseModel):
    question: str


@app.post("/ask")
def ask(req: AskRequest):
    if not os.getenv("ANTHROPIC_API_KEY"):
        return JSONResponse(
            {"error": "ANTHROPIC_API_KEY not set; /ask is disabled (use /query for raw SQL)"},
            status_code=503,
        )
    started = time.perf_counter()
    try:
        answer = agent.ask(req.question, _history, lambda sql: run_sql(sql) or "{}")
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return {
        "answer": answer,
        "turns": len(_history),
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


# ---------------------------------------------------------------------------
# Lifecycle hooks (:9000) — how the platform builds and drives the MicroVM
# ---------------------------------------------------------------------------

hooks = FastAPI(title="lifecycle hooks")


def _warm() -> int:
    """Touch the baked store so its pages are in memory when Lambda snapshots.

    Runs a real aggregation, not just a count: the platform samples which
    memory pages the snapshot actually uses, so warming the same access
    paths the app will use makes future launches faster.
    """
    run_sql("SELECT count() FROM demo.hits")
    run_sql(
        "SELECT RegionID, count() FROM demo.hits GROUP BY RegionID ORDER BY 2 DESC LIMIT 10"
    )
    return int(run_sql("SELECT count() FROM demo.hits", "TabSeparated").strip())


@hooks.post(f"{HOOK_PREFIX}/ready")
def ready():
    """Build-time gate: return 200 only once the engine is warm.

    Lambda polls this during create-microvm-image and takes the snapshot
    after the first 200 — this is what makes every launch start hot.
    """
    try:
        rows = _warm()
    except Exception as exc:
        logger.warning("ready: store not warm yet: %s", exc)
        return JSONResponse({"status": "not_ready"}, status_code=503)
    logger.info("ready: engine warm, %d baked rows", rows)
    return {"status": "ok", "baked_rows": rows}


@hooks.post(f"{HOOK_PREFIX}/validate")
def validate():
    """Build-time check: exercise a representative query against the snapshot."""
    try:
        run_sql(
            "SELECT EventDate, count() FROM demo.hits GROUP BY EventDate ORDER BY EventDate LIMIT 5"
        )
    except Exception as exc:
        logger.warning("validate: %s", exc)
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return {"status": "ok"}


def _reseed(event: str) -> dict:
    """Re-arm randomness after launch/resume.

    A snapshot freezes process memory, so every MicroVM launched from it
    starts with identical RNG state. Reseed and mint a fresh boot id so each
    instance is distinguishable and generates unique values.
    """
    global _boot_id
    random.seed()  # reseeds from os.urandom
    _boot_id = uuid.uuid4().hex
    logger.info("%s: new boot_id=%s", event, _boot_id)
    return {"status": "ok", "boot_id": _boot_id}


@hooks.post(f"{HOOK_PREFIX}/run")
def on_run():
    """Fires once when a MicroVM is launched from the snapshot."""
    return _reseed("run")


@hooks.post(f"{HOOK_PREFIX}/resume")
def on_resume():
    """Fires on SUSPENDED -> RUNNING. Disk and memory state are already back."""
    return _reseed("resume")


@hooks.post(f"{HOOK_PREFIX}/suspend")
def on_suspend():
    """Fires before RUNNING -> SUSPENDED. MergeTree data is already durable
    on the VM disk, so there is nothing to checkpoint — just log."""
    logger.info("suspend: boot_id=%s", _boot_id)
    return {"status": "ok"}


@hooks.post(f"{HOOK_PREFIX}/terminate")
def on_terminate():
    logger.info("terminate: closing chDB session")
    with _session_lock:
        _session.close()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Run both servers on one event loop
# ---------------------------------------------------------------------------


async def _serve() -> None:
    servers = [
        uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=APP_PORT, log_level="info")),
        uvicorn.Server(uvicorn.Config(hooks, host="0.0.0.0", port=HOOKS_PORT, log_level="info")),
    ]
    # uvicorn installs one signal handler per server and the second clobbers
    # the first; replace both with a single handler that stops both servers.
    for server in servers:
        server.install_signal_handlers = lambda: None

    loop = asyncio.get_running_loop()

    def _stop() -> None:
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    logger.info("app on :%d, lifecycle hooks on :%d, store at %s", APP_PORT, HOOKS_PORT, DATA_PATH)
    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(_serve())
