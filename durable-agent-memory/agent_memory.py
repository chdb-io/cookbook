"""A small, explicit agent-memory store on chDB Durable.

The commands are intentionally separate so each invocation opens the durable
object from storage again:

    python agent_memory.py init
    python agent_memory.py remember
    python agent_memory.py recall
    python agent_memory.py revise
    python agent_memory.py history

Set CHDB_DURABLE_URL to an S3, GCS, or Azure namespace to recover the same
object on another machine. The default local: URL is for a single-host demo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import uuid
from datetime import datetime, timezone

from chdb import durable as cd


DEFAULT_URL = "local:/tmp/chdb-durable-agent-memory"
DEFAULT_OBJECT_ID = "acme-checkout-api"
PROJECT_ID = "acme/checkout-api"
MEMORY_ID = "mem-deploy-region"
FIRST_MEMORY = "Deploy checkout-api to eu-west-1."
REVISED_MEMORY = "Deploy checkout-api to eu-central-1."


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS memories (
        id            String,
        content       String CODEC(ZSTD(3)),
        kind          LowCardinality(String),
        source        LowCardinality(String),
        source_ref    String,
        project_id    String,
        privacy       LowCardinality(String),
        tags          Array(String),
        embedding     Array(Float32),
        status        LowCardinality(String),
        pinned        UInt8,
        revises_id    String,
        conflict_with Array(String),
        created_at    DateTime64(3, 'UTC'),
        updated_at    DateTime64(3, 'UTC')
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY id
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_history (
        memory_id String,
        version   UInt32,
        op        LowCardinality(String),
        content   String CODEC(ZSTD(3)),
        edited_by String,
        edited_at DateTime64(3, 'UTC'),
        prev_id   String,
        note      String
    )
    ENGINE = MergeTree
    ORDER BY (memory_id, version, edited_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_transcripts (
        id         String,
        session_id String,
        agent      LowCardinality(String),
        project_id String,
        role       LowCardinality(String),
        text       String CODEC(ZSTD(3)),
        text_hash  String,
        meta_json  String CODEC(ZSTD(3)),
        created_at DateTime64(3, 'UTC')
    )
    ENGINE = MergeTree
    ORDER BY (session_id, created_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id           String,
        kind         LowCardinality(String),
        agent        LowCardinality(String),
        project_id   String,
        memory_id    String,
        message      String,
        payload_json String CODEC(ZSTD(3)),
        created_at   DateTime64(3, 'UTC')
    )
    ENGINE = MergeTree
    PARTITION BY toYYYYMMDD(created_at)
    ORDER BY (created_at, kind, id)
    TTL toDateTime(created_at) + INTERVAL 30 DAY
    """,
]


def utc_now() -> str:
    """Return a value that can be embedded as a stable DateTime64 literal."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def sql_string(value: str) -> str:
    """Quote the fixed demo values used below as ClickHouse string literals."""
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f"'{escaped}'"


def sql_strings(values: list[str]) -> str:
    return "[" + ", ".join(sql_string(value) for value in values) + "]"


def namespace(url: str, owner: str | None = None) -> cd.Namespace:
    writer = owner or f"{socket.gethostname()}-{os.getpid()}"
    return cd.Namespace(url, owner=writer, db="mem")


def scalar(obj: cd.DurableObject, sql: str) -> str:
    return obj.query(sql, "CSV").data().strip().strip('"')


def init_schema(url: str, object_id: str) -> None:
    obj = namespace(url, "schema-bootstrap").open(object_id)
    try:
        if scalar(obj, "SELECT count() FROM system.tables WHERE database=currentDatabase() "
                       "AND name IN ('memories', 'memory_history', 'raw_transcripts', 'events')") == "4":
            print("schema already exists")
            return
        for statement in SCHEMA:
            obj.execute(statement)
        base = obj.checkpoint()
        print(f"schema checkpoint: {base}")
    finally:
        obj.close()


def remember(url: str, object_id: str) -> None:
    obj = namespace(url).open(object_id)
    try:
        if scalar(obj, f"SELECT count() FROM memories FINAL WHERE id={sql_string(MEMORY_ID)}") != "0":
            print(f"memory already exists: {MEMORY_ID}")
            return

        transcript = "User confirmed: deploy checkout-api to eu-west-1."
        evidence_id = uuid.uuid4().hex
        created_at = utc_now()

        obj.execute(
            "INSERT INTO raw_transcripts "
            "(id, session_id, agent, project_id, role, text, text_hash, meta_json, created_at) VALUES ("
            f"{sql_string(evidence_id)}, {sql_string('session-42')}, {sql_string('coding-agent')}, "
            f"{sql_string(PROJECT_ID)}, {sql_string('user')}, {sql_string(transcript)}, "
            f"{sql_string(hashlib.sha256(transcript.encode()).hexdigest())}, "
            f"{sql_string(json.dumps({'source': 'chat'}))}, {sql_string(created_at)})"
        )
        obj.execute(
            "INSERT INTO memories "
            "(id, content, kind, source, source_ref, project_id, privacy, tags, embedding, "
            "status, pinned, revises_id, conflict_with, created_at, updated_at) VALUES ("
            f"{sql_string(MEMORY_ID)}, {sql_string(FIRST_MEMORY)}, {sql_string('decision')}, "
            f"{sql_string('agent_remember')}, {sql_string('transcript:' + evidence_id)}, "
            f"{sql_string(PROJECT_ID)}, {sql_string('private')}, "
            f"{sql_strings(['deployment', 'region'])}, [], {sql_string('active')}, 0, "
            f"{sql_string('')}, [], {sql_string(created_at)}, {sql_string(created_at)})"
        )
        obj.execute(
            "INSERT INTO memory_history "
            "(memory_id, version, op, content, edited_by, edited_at, prev_id, note) VALUES ("
            f"{sql_string(MEMORY_ID)}, 1, {sql_string('expand')}, {sql_string(FIRST_MEMORY)}, "
            f"{sql_string('coding-agent')}, {sql_string(created_at)}, {sql_string('')}, "
            f"{sql_string('promoted from transcript')})"
        )
        obj.execute(
            "INSERT INTO events VALUES ("
            f"{sql_string(uuid.uuid4().hex)}, {sql_string('memory.expand')}, "
            f"{sql_string('coding-agent')}, {sql_string(PROJECT_ID)}, {sql_string(MEMORY_ID)}, "
            f"{sql_string('memory committed')}, "
            f"{sql_string(json.dumps({'source_ref': 'transcript:' + evidence_id}))}, "
            f"{sql_string(created_at)})"
        )

        wal = obj.flush()
        print(f"remembered after durable flush: {wal}")
    finally:
        obj.close()


def recall(url: str, object_id: str) -> None:
    obj = namespace(url).open(object_id, read_only=True)
    try:
        rows = obj.query(
            """
            SELECT id, content, kind, tags, source_ref
            FROM memories FINAL
            WHERE project_id = 'acme/checkout-api'
              AND status = 'active'
              AND (positionCaseInsensitiveUTF8(content, 'deploy') > 0
                   OR has(tags, 'deployment'))
            ORDER BY pinned DESC, updated_at DESC
            LIMIT 5
            """,
            "JSONEachRow",
        ).data()
        print(rows or "no memory matched")
    finally:
        obj.close()


def revise(url: str, object_id: str) -> None:
    obj = namespace(url, "revision-worker").open(object_id)
    try:
        raw = obj.query(
            f"""
            SELECT id, content, kind, source, source_ref, project_id, privacy, tags,
                   pinned, toString(created_at) AS created_at
            FROM memories FINAL
            WHERE id = {sql_string(MEMORY_ID)}
            """,
            "JSONEachRow",
        ).data().strip()
        if not raw:
            raise RuntimeError("run the remember command before revise")
        current = json.loads(raw)
        if current["content"] == REVISED_MEMORY:
            print("memory already contains the revision")
            return

        revised_at = utc_now()
        version = int(scalar(
            obj,
            f"SELECT coalesce(max(version), 0) + 1 FROM memory_history "
            f"WHERE memory_id={sql_string(MEMORY_ID)}",
        ))
        obj.execute(
            "INSERT INTO memories "
            "(id, content, kind, source, source_ref, project_id, privacy, tags, embedding, "
            "status, pinned, revises_id, conflict_with, created_at, updated_at) VALUES ("
            f"{sql_string(current['id'])}, {sql_string(REVISED_MEMORY)}, "
            f"{sql_string(current['kind'])}, {sql_string(current['source'])}, "
            f"{sql_string(current['source_ref'])}, {sql_string(current['project_id'])}, "
            f"{sql_string(current['privacy'])}, {sql_strings(current['tags'])}, [], "
            f"{sql_string('active')}, {int(current['pinned'])}, {sql_string(MEMORY_ID)}, [], "
            f"{sql_string(current['created_at'])}, {sql_string(revised_at)})"
        )
        obj.execute(
            "INSERT INTO memory_history VALUES ("
            f"{sql_string(MEMORY_ID)}, {version}, {sql_string('revise')}, "
            f"{sql_string(REVISED_MEMORY)}, {sql_string('release-agent')}, "
            f"{sql_string(revised_at)}, {sql_string(MEMORY_ID)}, "
            f"{sql_string('region migration completed')})"
        )
        obj.execute(
            "INSERT INTO events VALUES ("
            f"{sql_string(uuid.uuid4().hex)}, {sql_string('memory.revise')}, "
            f"{sql_string('release-agent')}, {sql_string(PROJECT_ID)}, {sql_string(MEMORY_ID)}, "
            f"{sql_string('memory revised')}, {sql_string(json.dumps({'version': version}))}, "
            f"{sql_string(revised_at)})"
        )

        base = obj.checkpoint()
        print(f"revision checkpoint: {base}")
    finally:
        obj.close()


def history(url: str, object_id: str) -> None:
    obj = namespace(url).open(object_id, read_only=True)
    try:
        rows = obj.query(
            f"SELECT version, op, content, edited_by, toString(edited_at) AS edited_at, note "
            f"FROM memory_history WHERE memory_id={sql_string(MEMORY_ID)} ORDER BY version",
            "PrettyCompact",
        ).data()
        print(rows)
    finally:
        obj.close()


def run_demo(url: str, object_id: str | None) -> None:
    # A new object makes the demo repeatable without deleting an existing brain.
    selected = object_id or f"demo-{uuid.uuid4().hex[:8]}"
    print(f"namespace: {url}")
    print(f"object:    {selected}")
    init_schema(url, selected)
    remember(url, selected)
    recall(url, selected)
    revise(url, selected)
    recall(url, selected)
    history(url, selected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("demo", "init", "remember", "recall", "revise", "history"),
    )
    parser.add_argument("--url", default=os.getenv("CHDB_DURABLE_URL", DEFAULT_URL))
    parser.add_argument("--object-id", default=os.getenv("CHDB_DURABLE_OBJECT_ID"))
    args = parser.parse_args()

    if args.command == "demo":
        run_demo(args.url, args.object_id)
        return

    object_id = args.object_id or DEFAULT_OBJECT_ID
    commands = {
        "init": init_schema,
        "remember": remember,
        "recall": recall,
        "revise": revise,
        "history": history,
    }
    commands[args.command](args.url, object_id)


if __name__ == "__main__":
    main()
