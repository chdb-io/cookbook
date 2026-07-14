#!/usr/bin/env bash
# Deploy the chDB analyst to Google Cloud Run.
#
# One command: `gcloud run deploy --source .` has Cloud Build produce the
# image (baking the dataset in the process) and rolls out a service that
# scales to zero. The script then smoke-tests it and reports the first-hit
# (cold) and steady-state (warm) latencies.
#
# Requires: gcloud authenticated against a project with billing; the script
# enables the run/cloudbuild/artifactregistry APIs if needed.
#
# Usage:
#   export ANTHROPIC_API_KEY=sk-...   # optional — omit to deploy /query only
#   ./deploy.sh                       # us-central1 by default; REGION=... to override
set -euo pipefail
cd "$(dirname "$0")"

REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-chdb-analyst}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"

PROJECT=$(gcloud config get-value project 2>/dev/null)
echo "==> project ${PROJECT}, region ${REGION}"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com --quiet

if [ -n "${ANTHROPIC_API_KEY}" ]; then
  # env var for a demo; use Secret Manager (--set-secrets) for production
  ENV_FLAG=(--set-env-vars "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}")
else
  # Cloud Run keeps existing env on update — clear it so unsetting the key
  # actually disables /ask on a redeploy (no-op on a first deploy)
  ENV_FLAG=(--remove-env-vars ANTHROPIC_API_KEY)
fi

# /query runs caller-supplied ClickHouse SQL, and chDB's url()/s3() table
# functions can reach the instance metadata server — so a public endpoint is
# an unauthenticated SSRF + credential-exfiltration path. Deploy private by
# default and call with an identity token; opt into public only with PUBLIC=1
# (fronting it with your own auth/authorization before doing so).
if [ "${PUBLIC:-}" = "1" ]; then
  echo "!! PUBLIC=1: deploying an unauthenticated endpoint that executes arbitrary SQL."
  echo "!! Anyone who can reach it can run url()/s3() against your metadata server."
  AUTH_FLAG=(--allow-unauthenticated)
  AUTH_HEADER=()
else
  AUTH_FLAG=(--no-allow-unauthenticated)
  AUTH_HEADER=(-H "Authorization: Bearer $(gcloud auth print-identity-token)")
fi

echo "==> building and deploying (Cloud Build bakes the 1M-row store into the image)"
gcloud run deploy "${SERVICE}" --source . --region "${REGION}" \
  --memory 4Gi --cpu 2 --concurrency 8 --min-instances 0 --max-instances 3 \
  "${AUTH_FLAG[@]}" "${ENV_FLAG[@]}" --quiet

URL=$(gcloud run services describe "${SERVICE}" --region "${REGION}" \
  --format 'value(status.url)')
echo "==> service ${URL}"

# curl computes its own timing (%{time_total}) — portable, no `date +%N`
# which BSD/macOS date doesn't support
echo "==> first hit (cold: instance start + engine init)"
curl -sf "${AUTH_HEADER[@]}" -w '    (%{time_total}s)\n' "${URL}/health"

echo "==> warm hits"
for _ in 1 2 3; do
  curl -sf "${AUTH_HEADER[@]}" -o /dev/null -w '    %{time_total}s\n' "${URL}/health"
done

cat <<EOF

Deployed$([ "${PUBLIC:-}" = "1" ] || echo " (private — pass an identity token; run with PUBLIC=1 for a public URL)"). Talk to the analyst:

  AUTH='${AUTH_HEADER[*]:+-H "Authorization: Bearer \$(gcloud auth print-identity-token)"}'
  curl -s \$AUTH ${URL}/query -H 'Content-Type: application/json' \\
    -d '{"sql": "SELECT RegionID, count() AS hits FROM demo.hits GROUP BY RegionID ORDER BY hits DESC LIMIT 5"}'
  curl -s \$AUTH ${URL}/ask -H 'Content-Type: application/json' \\
    -d '{"question": "Which regions drive the most traffic?"}'

Scale-to-zero: with no traffic the instance count drops to 0 and you pay
nothing; the next request pays the cold start you measured above.

Teardown: ./teardown.sh
EOF
