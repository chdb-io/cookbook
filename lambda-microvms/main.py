"""chDB on AWS Lambda MicroVMs — a private SQL engine per session.

The analyst itself is the published `chdb-serverless` package: its FastAPI app
(/health /query /ask over an embedded chDB store, with the pluggable-LLM
agent). This file adds only what's specific to MicroVMs — a second server for
the six lifecycle hooks, run in the *same process* as the app:

  :8080  the app (chdb_serverless.server.app) — the port the MicroVM proxy
         routes client traffic to.
  :9000  the six Lambda MicroVMs lifecycle hooks, under the platform path
         prefix /aws/lambda-microvms/runtime/v1.

The single-process design is deliberate: the /ready hook warms the chDB store
*in this process* before the platform snapshots the VM, so the snapshot
captures a hot engine. The hooks reuse the app's own store handle
(`chdb_serverless.server._store`) — one engine, one session, warmed once.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import sys
import uuid

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# The app and the exact store it serves from — reusing the store keeps this to
# one chDB session (chDB is single-writer), so /ready warms what the app uses.
# `pkgsrv` is imported as a module (not `from ... import _instance_id`) so a
# reseed can rebind the app's per-instance id and have /health see it.
import chdb_serverless.server as pkgsrv
from chdb_serverless.server import app, _store

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sql-sandbox")

HOOKS_PORT = int(os.getenv("MICROVM_HOOKS_PORT", "9000"))
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"
_boot_id = uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Lifecycle hooks (:9000) — how the platform builds and drives the MicroVM
# ---------------------------------------------------------------------------

hooks = FastAPI(title="lifecycle hooks")


def _warm() -> int:
    """Touch the baked store so its pages are in memory when Lambda snapshots.

    Runs a real aggregation, not just a count: the platform samples which
    memory pages the snapshot actually uses, so warming the same access paths
    the app will use makes future launches faster.
    """
    _store.query("SELECT count() FROM demo.hits")
    _store.query(
        "SELECT RegionID, count() FROM demo.hits GROUP BY RegionID ORDER BY 2 DESC LIMIT 10"
    )
    return int(_store.query("SELECT count() FROM demo.hits", "TabSeparated").strip())


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
        _store.query(
            "SELECT EventDate, count() FROM demo.hits GROUP BY EventDate ORDER BY EventDate LIMIT 5"
        )
    except Exception as exc:
        logger.warning("validate: %s", exc)
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return {"status": "ok"}


def _reseed(event: str) -> dict:
    """Re-arm randomness and per-instance identity after launch/resume.

    A snapshot freezes process memory, so every MicroVM launched from it
    starts with identical RNG state and a frozen instance id. Reseed and mint a
    fresh boot id so each instance is distinguishable and generates unique
    values. The app on :8080 reports identity via `/health` ("instance"), and
    that value is baked into the snapshot too — so push the fresh id into the
    package server's module global here, making a client's `/health` reflect
    the live instance rather than the one captured at snapshot time.
    """
    global _boot_id
    random.seed()  # reseeds from os.urandom
    _boot_id = uuid.uuid4().hex
    pkgsrv._instance_id = _boot_id[:8]
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
    """Fires before the VM is destroyed. The store is durable on disk and the
    process is about to exit, so there's nothing to flush — just log."""
    logger.info("terminate: boot_id=%s", _boot_id)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Run both servers on one event loop
# ---------------------------------------------------------------------------


async def _serve() -> None:
    app_port = int(os.getenv("PORT", "8080"))
    servers = [
        uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=app_port, log_level="info")),
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

    logger.info("app on :%d, lifecycle hooks on :%d", app_port, HOOKS_PORT)
    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(_serve())
