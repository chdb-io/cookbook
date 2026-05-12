"""Static migration helper: scan a Python tree for DuckDB API calls and propose chDB equivalents.

Usage:
    python migrate.py path/to/project
    python migrate.py path/to/project --apply        # rewrite simple cases in place
    python migrate.py path/to/project --dialect-only # only flag SQL dialect risks

The script does not change behavior — it produces a checklist a human can audit.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass
from typing import Iterable


# Mechanical Python-side rewrites: (regex, replacement, label).
PY_RULES = [
    (r"\bimport duckdb\b",
     "import chdb\nfrom chdb import session as chdb_session",
     "import"),
    (r"\bduckdb\.connect\(\s*(['\"]?:memory:['\"]?)?\s*\)",
     "chdb_session.Session()",
     "connect"),
    (r"\bduckdb\.connect\(\s*(['\"][^'\"]+['\"])\s*\)",
     r"chdb_session.Session(\1)",
     "connect (persistent)"),
    (r"\.execute\(([^)]+)\)\.df\(\)",
     r".query(\1, 'DataFrame')",
     "execute().df() → query(.., 'DataFrame')"),
    (r"\.execute\(([^)]+)\)\.fetchall\(\)",
     r".query(\1, 'CSV')",
     "execute().fetchall() — review return shape"),
    (r"\.register\(\s*['\"]([^'\"]+)['\"]\s*,\s*(\w+)\s*\)",
     "# chDB: drop register(); reference the DataFrame as Python(\\2) in SQL",
     "register(name, df) → Python(df) table function"),
]

# SQL-side patterns that change between DuckDB and ClickHouse SQL.
# Negative lookbehind (?<![.\w]) skips Python method calls like `pd.read_parquet(...)`.
SQL_RULES = [
    (r"(?<![.\w])\bread_parquet\(\s*(['\"][^'\"]+['\"])\s*\)",
     r"file(\1, 'Parquet')",
     "read_parquet"),
    (r"(?<![.\w])\bread_csv\(\s*(['\"][^'\"]+['\"])(?:[^)]*)?\)",
     r"file(\1, 'CSV')",
     "read_csv"),
    (r"(?<![.\w])\bread_json\(\s*(['\"][^'\"]+['\"])(?:[^)]*)?\)",
     r"file(\1, 'JSONEachRow')",
     "read_json"),
    (r"\bdate_trunc\(\s*['\"]hour['\"]\s*,\s*([^)]+)\)",
     r"toStartOfHour(\1)",
     "date_trunc('hour', x)"),
    (r"\bdate_trunc\(\s*['\"]day['\"]\s*,\s*([^)]+)\)",
     r"toStartOfDay(\1)",
     "date_trunc('day', x)"),
    (r"\bdate_trunc\(\s*['\"]month['\"]\s*,\s*([^)]+)\)",
     r"toStartOfMonth(\1)",
     "date_trunc('month', x)"),
    (r"\bapprox_count_distinct\(",
     r"uniqHLL12(",
     "approx_count_distinct"),
    (r"\bstrftime\(\s*([^,]+),\s*(['\"][^'\"]+['\"])\s*\)",
     r"formatDateTime(\1, \2)",
     "strftime"),
    (r"\bregexp_matches\(\s*([^,]+),\s*([^)]+)\)",
     r"match(\1, \2)",
     "regexp_matches → match"),
]

# Hard cases that need human review.
DIALECT_REVIEW = [
    ("PIVOT", "DuckDB PIVOT has no direct chDB equivalent — rewrite with conditional aggregation (`sumIf`, `countIf`)."),
    ("UNNEST", "Replace with `arrayJoin()`."),
    ("LIST_", "DuckDB LIST_* functions usually map to `array*` functions in chDB; review case-by-case."),
    ("STRUCT", "DuckDB STRUCT type maps to chDB `Tuple()` or `Map()` depending on schema-ness."),
    ("INSTALL ", "chDB has no INSTALL/LOAD extension mechanism — features that DuckDB provides via extensions (httpfs, spatial, ML) are either built in (httpfs, S3) or unsupported (spatial extension, FUGUE)."),
    ("CREATE INDEX", "chDB does not support secondary indexes — use ORDER BY in MergeTree DDL for primary-key-like behavior."),
]


@dataclass
class Hit:
    path: str
    line: int
    label: str
    before: str
    after: str


def scan_file(path: str, rules) -> Iterable[Hit]:
    with open(path) as f:
        for n, line in enumerate(f, start=1):
            for pattern, replacement, label in rules:
                m = re.search(pattern, line)
                if m:
                    after = re.sub(pattern, replacement, line.rstrip("\n"))
                    yield Hit(path, n, label, line.rstrip("\n"), after)


def scan_dialect(path: str) -> Iterable[Hit]:
    with open(path) as f:
        for n, line in enumerate(f, start=1):
            for needle, note in DIALECT_REVIEW:
                if needle in line:
                    yield Hit(path, n, f"DIALECT REVIEW: {needle}", line.rstrip("\n"), note)


def walk(root: str):
    for dirpath, _, names in os.walk(root):
        if any(skip in dirpath for skip in [".venv", "__pycache__", ".git", "node_modules"]):
            continue
        for name in names:
            if name.endswith((".py", ".sql", ".ipynb")):
                yield os.path.join(dirpath, name)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("root", help="Directory to scan")
    p.add_argument("--apply", action="store_true", help="Rewrite simple cases in place (back up first)")
    p.add_argument("--dialect-only", action="store_true")
    args = p.parse_args()

    if not os.path.isdir(args.root):
        sys.exit(f"Not a directory: {args.root}")

    by_file = {}
    rules = SQL_RULES if args.dialect_only else (PY_RULES + SQL_RULES)
    for path in walk(args.root):
        hits = list(scan_file(path, rules)) + list(scan_dialect(path))
        if hits:
            by_file[path] = hits

    if not by_file:
        print("No DuckDB API usage found. Either you're already on chDB, or this tree does not query a database.")
        return

    total = sum(len(h) for h in by_file.values())
    print(f"Found {total} migration touch points across {len(by_file)} files:\n")
    for path, hits in sorted(by_file.items()):
        print(f"--- {path}")
        for h in hits:
            print(f"  L{h.line:>4}  {h.label}")
            print(f"        before:  {h.before.strip()}")
            print(f"        after :  {h.after.strip()[:140]}")
        print()

    if args.apply:
        print("\n--apply: rewriting files in place (originals saved to *.bak)")
        for path, hits in by_file.items():
            simple = [h for h in hits if not h.label.startswith("DIALECT")]
            if not simple:
                continue
            with open(path) as f:
                text = f.read()
            for pattern, replacement, _ in (PY_RULES + SQL_RULES):
                text = re.sub(pattern, replacement, text)
            with open(path + ".bak", "w") as f:
                f.write(open(path).read())
            with open(path, "w") as f:
                f.write(text)
            print(f"  rewrote {path} ({len(simple)} edits)")
        print("\nReview the diff against *.bak files, run your tests, then delete the .bak files.")


if __name__ == "__main__":
    main()
