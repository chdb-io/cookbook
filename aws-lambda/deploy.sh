#!/usr/bin/env bash
# Deploy the chDB analyst as a classic AWS Lambda container function.
#
# Creates (all in your account):
#   * an ECR repository and the image (built locally with docker/podman —
#     chDB exceeds the 250 MB zip limit, so container packaging is the way)
#   * a minimal execution role
#   * one Lambda function (4 GB, x86_64) with a public Function URL
#
# The image is the shared serverless image plus one line: the AWS Lambda
# Web Adapter, which translates invocations into HTTP against the same
# uvicorn app. On any other platform that line is inert.
#
# Requires: AWS CLI v2 with credentials, docker or podman.
#
# Usage:
#   export ANTHROPIC_API_KEY=sk-...   # optional — omit to deploy /query only
#   ./deploy.sh                       # us-west-2 by default; REGION=... to override
set -euo pipefail
cd "$(dirname "$0")"

REGION="${REGION:-us-west-2}"
NAME="${NAME:-chdb-analyst}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
DOCKER="${DOCKER:-docker}"   # DOCKER=podman works too

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${ECR}/${NAME}:v1"
echo "==> account ${ACCOUNT}, region ${REGION}"

# --- 1. registry + image ------------------------------------------------------
aws ecr describe-repositories --repository-names "${NAME}" --region "${REGION}" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "${NAME}" --region "${REGION}" >/dev/null
aws ecr get-login-password --region "${REGION}" \
  | ${DOCKER} login --username AWS --password-stdin "${ECR}"
echo "==> building the image (bakes the 1M-row store; needs an amd64 builder)"
${DOCKER} build --platform linux/amd64 -t "${IMAGE}" .
${DOCKER} push "${IMAGE}" | tail -1

# --- 2. execution role ---------------------------------------------------------
ROLE="${NAME}-exec-role"
if ! aws iam get-role --role-name "${ROLE}" >/dev/null 2>&1; then
  aws iam create-role --role-name "${ROLE}" \
    --tags Key=chdb-cookbook,Value=true \
    --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
                   "Principal": {"Service": "lambda.amazonaws.com"},
                   "Action": "sts:AssumeRole"}]}' >/dev/null
  sleep 10   # IAM propagation before the function can assume it
fi
# attaching an already-attached managed policy is a no-op, so run it every
# time — otherwise a run that created the role but failed here would leave it
# permanently without log permissions
aws iam attach-role-policy --role-name "${ROLE}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE}"

# --- 3. the function + a public URL -------------------------------------------
ENV_VARS="Variables={PORT=8080"
[ -n "${ANTHROPIC_API_KEY}" ] && ENV_VARS+=",ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
ENV_VARS+="}"
# Lambda's container filesystem is read-only except /tmp, and chDB needs a
# writable data directory (status file, locks). The command override copies
# the baked store to /tmp at boot — the image itself stays byte-identical
# to the Cloud Run / Container Apps recipes.
# rm the destination first: Lambda keeps /tmp across an execution-environment
# reset, so a bare `cp -r` onto an existing dir would nest chdb-data/chdb-data
BOOT_CMD='rm -rf /tmp/chdb-data; cp -r /app/chdb-data /tmp/chdb-data; CHDB_DATA_PATH=/tmp/chdb-data exec python main.py'
if aws lambda get-function --function-name "${NAME}" --region "${REGION}" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "${NAME}" \
    --image-uri "${IMAGE}" --region "${REGION}" >/dev/null
  aws lambda wait function-updated-v2 --function-name "${NAME}" --region "${REGION}"
  # re-apply env on redeploy too, so setting/rotating ANTHROPIC_API_KEY takes effect
  aws lambda update-function-configuration --function-name "${NAME}" \
    --environment "${ENV_VARS}" --region "${REGION}" >/dev/null
else
  aws lambda create-function --function-name "${NAME}" \
    --package-type Image --code "ImageUri=${IMAGE}" \
    --role "${ROLE_ARN}" --architectures x86_64 \
    --memory-size 4096 --timeout 120 --ephemeral-storage Size=1024 \
    --image-config "{\"Command\":[\"sh\",\"-c\",\"${BOOT_CMD}\"]}" \
    --environment "${ENV_VARS}" --tags chdb-cookbook=true --region "${REGION}" >/dev/null
fi
aws lambda wait function-active-v2 --function-name "${NAME}" --region "${REGION}"

URL=$(aws lambda get-function-url-config --function-name "${NAME}" \
        --region "${REGION}" --query FunctionUrl --output text 2>/dev/null) \
  || URL=$(aws lambda create-function-url-config --function-name "${NAME}" \
             --auth-type NONE --region "${REGION}" --query FunctionUrl --output text)
# always (re)assert the permissions, whether or not the URL already existed —
# a public (NONE) Function URL needs both InvokeFunctionUrl and InvokeFunction
# under AWS's current model. add-permission errors if the statement-id is
# already there, so the || true makes each idempotent and self-healing.
aws lambda add-permission --function-name "${NAME}" \
  --action lambda:InvokeFunctionUrl --principal '*' \
  --function-url-auth-type NONE --statement-id public-url \
  --region "${REGION}" >/dev/null 2>&1 || true
aws lambda add-permission --function-name "${NAME}" \
  --action lambda:InvokeFunction --principal '*' \
  --function-url-auth-type NONE --statement-id public-url-invoke \
  --region "${REGION}" >/dev/null 2>&1 || true
URL="${URL%/}"
echo "==> function URL ${URL}"

# --- 4. smoke test with timings -------------------------------------------------
python3 - "$URL" <<'EOF'
import sys, time, urllib.request
url = sys.argv[1]
t0 = time.time()
body = urllib.request.urlopen(f"{url}/health", timeout=180).read().decode()
print(f"==> first hit (cold: sandbox init + engine init): {(time.time()-t0)*1000:.0f} ms")
print(f"    {body}")
for _ in range(3):
    t0 = time.time()
    urllib.request.urlopen(f"{url}/health", timeout=30).read()
    print(f"==> warm hit: {(time.time()-t0)*1000:.0f} ms")
EOF

cat <<EOF

Deployed. Talk to the analyst:

  curl -s ${URL}/query -H 'Content-Type: application/json' \\
    -d '{"sql": "SELECT RegionID, count() AS hits FROM demo.hits GROUP BY RegionID ORDER BY hits DESC LIMIT 5"}'
  curl -s ${URL}/ask -H 'Content-Type: application/json' \\
    -d '{"question": "Which regions drive the most traffic?"}'

Idle costs nothing (per-request billing); the next request after an idle
gap pays the cold start you measured above.

Teardown: ./teardown.sh
EOF
