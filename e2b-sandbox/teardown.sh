#!/usr/bin/env bash
# Kill this demo's sandboxes (running and paused) and delete the "chdb"
# template (override with CHDB_TEMPLATE=...). Needs the E2B CLI:
# `npm i -g @e2b/cli`, then `e2b auth login`.
set -euo pipefail

TEMPLATE="${CHDB_TEMPLATE:-chdb}"
METADATA="demo=chdb-e2b-cookbook"   # set by analyst.py on every sandbox it creates

command -v e2b >/dev/null 2>&1 || { echo "e2b CLI not found — npm i -g @e2b/cli" >&2; exit 1; }

# --state defaults to running, so sweep paused ones separately.
echo "==> killing running demo sandboxes (metadata ${METADATA})"
e2b sandbox kill --all --metadata "${METADATA}" || true   # non-zero when nothing matches

echo "==> killing paused demo sandboxes"
e2b sandbox kill --all --state paused --metadata "${METADATA}" || true

echo "==> deleting template ${TEMPLATE}"
if ! e2b template delete "${TEMPLATE}" -y; then   # -y skips the confirmation prompt
  echo "    not deleted — may not exist, or sandboxes still use it (e2b template list)" >&2
fi

echo "done."
