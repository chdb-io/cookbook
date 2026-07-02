#!/usr/bin/env bash
# Deploy the chDB SQL sandbox to AWS Lambda MicroVMs.
#
# Creates (all idempotent, all in your account):
#   * a private S3 bucket for the code artifact
#   * a build role (Lambda assumes it to fetch the artifact and write build logs)
#   * an execution role (runtime CloudWatch logs)
#   * a MicroVM image with the chDB store baked in and lifecycle hooks enabled
#   * one running MicroVM with a dedicated HTTPS endpoint
#
# Requires AWS CLI >= 2.35.12 (the first version that ships `lambda-microvms`)
# and permissions for lambda-microvms:*, iam role management, and the bucket.
#
# Usage:
#   ./deploy.sh                  # us-west-2, image + one MicroVM
#   REGION=us-east-1 ./deploy.sh
set -euo pipefail

REGION="${REGION:-us-west-2}"
NAME="${NAME:-chdb-sql-sandbox}"
# Optional: bake an Anthropic API key into the image env so /ask (the analyst
# agent) works. Fine for a demo; use a secrets manager for production.
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${NAME}-artifacts-${ACCOUNT}-${REGION}"
BUILD_ROLE="${NAME}-build-role"
EXEC_ROLE="${NAME}-exec-role"

echo "==> account ${ACCOUNT}, region ${REGION}"

# --- 1. artifact bucket ------------------------------------------------------
if ! aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  if [ "${REGION}" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}"
  else
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
      --create-bucket-configuration "LocationConstraint=${REGION}"
  fi
  aws s3api put-public-access-block --bucket "${BUCKET}" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
fi
echo "==> bucket s3://${BUCKET}"

# --- 2. IAM roles ------------------------------------------------------------
TRUST=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": ["sts:AssumeRole", "sts:TagSession"],
    "Condition": { "StringEquals": { "aws:SourceAccount": "${ACCOUNT}" } }
  }]
}
EOF
)

ensure_role() {  # name policy-json
  local role="$1" policy="$2"
  if ! aws iam get-role --role-name "${role}" >/dev/null 2>&1; then
    aws iam create-role --role-name "${role}" \
      --assume-role-policy-document "${TRUST}" >/dev/null
  fi
  aws iam put-role-policy --role-name "${role}" \
    --policy-name "${role}-policy" --policy-document "${policy}"
}

BUILD_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/*" },
    { "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/lambda-microvms/*" },
    { "Effect": "Allow", "Action": ["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"],
      "Resource": "*" }
  ]
}
EOF
)

EXEC_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/lambda-microvms/*" }
  ]
}
EOF
)

ensure_role "${BUILD_ROLE}" "${BUILD_POLICY}"
ensure_role "${EXEC_ROLE}" "${EXEC_POLICY}"
BUILD_ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${BUILD_ROLE}"
EXEC_ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${EXEC_ROLE}"
echo "==> roles ${BUILD_ROLE}, ${EXEC_ROLE}"

# --- 3. package + upload -----------------------------------------------------
ZIP=$(mktemp -d)/app.zip
zip -q -j "${ZIP}" Dockerfile main.py agent.py init_db.py requirements.txt
aws s3 cp --quiet "${ZIP}" "s3://${BUCKET}/app.zip"
echo "==> uploaded s3://${BUCKET}/app.zip"

# --- 4. MicroVM image --------------------------------------------------------
BASE_IMAGE_ARN=$(aws lambda-microvms list-managed-microvm-images --region "${REGION}" \
  --query 'items[0].imageArn' --output text)
if [ -z "${BASE_IMAGE_ARN}" ] || [ "${BASE_IMAGE_ARN}" = "None" ]; then
  echo "no managed base image in ${REGION} — is Lambda MicroVMs available there?"; exit 1
fi
echo "==> base image ${BASE_IMAGE_ARN}"

HOOKS=$(cat <<'EOF'
{
  "port": 9000,
  "microvmImageHooks": {
    "ready": "ENABLED", "readyTimeoutInSeconds": 120,
    "validate": "ENABLED", "validateTimeoutInSeconds": 120
  },
  "microvmHooks": {
    "run": "ENABLED", "runTimeoutInSeconds": 5,
    "resume": "ENABLED", "resumeTimeoutInSeconds": 5,
    "suspend": "ENABLED", "suspendTimeoutInSeconds": 10,
    "terminate": "ENABLED", "terminateTimeoutInSeconds": 10
  }
}
EOF
)

ENV_VARS=$(python3 - <<EOF
import json, os
env = {"CHDB_DATA_PATH": "/app/chdb-data", "MICROVM_HOOKS_PORT": "9000"}
if os.environ.get("ANTHROPIC_API_KEY"):
    env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
print(json.dumps(env))
EOF
)

IMAGE_ARN="arn:aws:lambda:${REGION}:${ACCOUNT}:microvm-image:${NAME}"
if aws lambda-microvms get-microvm-image --image-identifier "${IMAGE_ARN}" \
     --region "${REGION}" >/dev/null 2>&1; then
  echo "==> image exists, pushing a new version"
  aws lambda-microvms update-microvm-image \
    --image-identifier "${IMAGE_ARN}" \
    --base-image-arn "${BASE_IMAGE_ARN}" \
    --build-role-arn "${BUILD_ROLE_ARN}" \
    --code-artifact "uri=s3://${BUCKET}/app.zip" \
    --hooks "${HOOKS}" \
    --environment-variables "${ENV_VARS}" \
    --region "${REGION}" >/dev/null
else
  aws lambda-microvms create-microvm-image \
    --name "${NAME}" \
    --description "chDB SQL sandbox (embedded ClickHouse per session)" \
    --base-image-arn "${BASE_IMAGE_ARN}" \
    --build-role-arn "${BUILD_ROLE_ARN}" \
    --code-artifact "uri=s3://${BUCKET}/app.zip" \
    --hooks "${HOOKS}" \
    --environment-variables "${ENV_VARS}" \
    --region "${REGION}" >/dev/null
fi

echo -n "==> building image (bakes the dataset, warms chDB, snapshots) "
VERSION="" STATE=""
for _ in $(seq 1 120); do
  read -r VERSION STATE <<< "$(aws lambda-microvms list-microvm-image-versions \
    --image-identifier "${IMAGE_ARN}" --region "${REGION}" \
    --query 'reverse(sort_by(items, &createdAt))[0].[imageVersion, state]' \
    --output text)"
  case "${STATE}" in
    SUCCESSFUL) echo " done (version ${VERSION})"; break ;;
    FAILED) echo; echo "build FAILED — list-microvm-image-builds carries stateReason;"
            echo "full logs in CloudWatch /aws/lambda-microvms/"; exit 1 ;;
    *) echo -n "."; sleep 15 ;;
  esac
done
[ "${STATE}" = "SUCCESSFUL" ] || { echo "timed out"; exit 1; }

# --- 5. run a MicroVM --------------------------------------------------------
RUN_OUT=$(aws lambda-microvms run-microvm \
  --image-identifier "${IMAGE_ARN}" \
  --image-version "${VERSION}" \
  --execution-role-arn "${EXEC_ROLE_ARN}" \
  --ingress-network-connectors "arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS" \
  --egress-network-connectors "arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS" \
  --idle-policy '{"autoResumeEnabled":true,"maxIdleDurationSeconds":900,"suspendedDurationSeconds":3600}' \
  --maximum-duration-in-seconds 28800 \
  --logging "{\"cloudWatch\":{\"logGroup\":\"/aws/lambda-microvms/${NAME}\"}}" \
  --region "${REGION}")
MICROVM_ID=$(echo "${RUN_OUT}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["microvmId"])')
ENDPOINT=$(echo "${RUN_OUT}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["endpoint"])')

echo -n "==> waiting for MicroVM ${MICROVM_ID} "
for _ in $(seq 1 60); do
  STATE=$(aws lambda-microvms get-microvm --microvm-identifier "${MICROVM_ID}" \
    --region "${REGION}" --query 'state' --output text)
  [ "${STATE}" = "RUNNING" ] && { echo " RUNNING"; break; }
  echo -n "."; sleep 5
done

# --- 6. smoke test -----------------------------------------------------------
TOKEN=$(aws lambda-microvms create-microvm-auth-token \
  --microvm-identifier "${MICROVM_ID}" \
  --expiration-in-minutes 60 \
  --allowed-ports '[{"port":8080}]' \
  --region "${REGION}" \
  --query 'authToken."X-aws-proxy-auth"' --output text)

echo "==> smoke test: GET /health"
curl -sf "https://${ENDPOINT}/health" -H "X-aws-proxy-auth: ${TOKEN}" | python3 -m json.tool

cat <<EOF

Deployed. Talk to your private SQL engine:

  export ENDPOINT=${ENDPOINT}
  export TOKEN=${TOKEN}

  # Raw SQL:
  curl -s "https://\${ENDPOINT}/query" -H "X-aws-proxy-auth: \${TOKEN}" \\
    -H 'Content-Type: application/json' \\
    -d '{"sql": "SELECT RegionID, count() AS hits FROM demo.hits GROUP BY RegionID ORDER BY hits DESC LIMIT 5"}'

  # Or ask the analyst agent (requires ANTHROPIC_API_KEY at deploy time):
  curl -s "https://\${ENDPOINT}/ask" -H "X-aws-proxy-auth: \${TOKEN}" \\
    -H 'Content-Type: application/json' \\
    -d '{"question": "Which regions drive the most traffic?"}'

Lifecycle:
  aws lambda-microvms suspend-microvm   --microvm-identifier ${MICROVM_ID} --region ${REGION}
  aws lambda-microvms resume-microvm    --microvm-identifier ${MICROVM_ID} --region ${REGION}
  aws lambda-microvms terminate-microvm --microvm-identifier ${MICROVM_ID} --region ${REGION}
EOF
