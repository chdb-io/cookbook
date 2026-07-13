#!/usr/bin/env bash
# Remove everything deploy.sh created: MicroVMs, the image (versions incur
# storage cost even with nothing running), the artifact bucket, and the roles.
set -euo pipefail

REGION="${REGION:-us-west-2}"
NAME="${NAME:-chdb-sql-sandbox}"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${NAME}-artifacts-${ACCOUNT}-${REGION}"
IMAGE_ARN="arn:aws:lambda:${REGION}:${ACCOUNT}:microvm-image:${NAME}"

echo "==> terminating MicroVMs of ${NAME}"
for id in $(aws lambda-microvms list-microvms --region "${REGION}" \
    --query "items[?contains(imageArn, ':microvm-image:${NAME}') && state != 'TERMINATED'].microvmId" \
    --output text); do
  echo "    terminate ${id}"
  aws lambda-microvms terminate-microvm --microvm-identifier "${id}" --region "${REGION}" >/dev/null || true
done

echo "==> deleting image ${IMAGE_ARN}"
if aws lambda-microvms get-microvm-image --image-identifier "${IMAGE_ARN}" \
     --region "${REGION}" >/dev/null 2>&1; then
  aws lambda-microvms delete-microvm-image --image-identifier "${IMAGE_ARN}" \
    --region "${REGION}" >/dev/null   # a real deletion error aborts loudly here
else
  echo "    (no image to delete)"
fi

echo "==> deleting bucket s3://${BUCKET}"
aws s3 rb "s3://${BUCKET}" --force >/dev/null 2>&1 || echo "    (no bucket to delete)"

for role in "${NAME}-build-role" "${NAME}-exec-role"; do
  echo "==> deleting role ${role}"
  aws iam delete-role-policy --role-name "${role}" --policy-name "${role}-policy" 2>/dev/null || true
  aws iam delete-role --role-name "${role}" 2>/dev/null || echo "    (no role to delete)"
done

echo "done."
