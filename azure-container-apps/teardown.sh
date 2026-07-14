#!/usr/bin/env bash
# Remove what deploy.sh created — the app, the environment, and the registry
# — by name, so a pre-existing resource group that held other resources is
# left intact. Only delete the group if it ends up empty (i.e. deploy.sh
# created it). Override the names with the same env vars deploy.sh uses.
set -euo pipefail

RG="${RESOURCE_GROUP:-chdb-analyst-rg}"
APP="${APP:-chdb-analyst}"
ENVIRONMENT="${ENVIRONMENT:-chdb-analyst-env}"

if ! az group show -n "${RG}" >/dev/null 2>&1; then
  echo "    (no resource group ${RG} to clean up)"; exit 0
fi

echo "==> deleting container app ${APP}"
az containerapp delete -g "${RG}" -n "${APP}" --yes -o none 2>/dev/null || echo "    (none)"

echo "==> deleting Container Apps environment ${ENVIRONMENT}"
az containerapp env delete -g "${RG}" -n "${ENVIRONMENT}" --yes -o none 2>/dev/null || echo "    (none)"

# recompute the exact registry name deploy.sh derived (subscription + RG),
# so we delete only our own registry — never a prefix match that could catch
# another deployment's registry in a shared group
SUB=$(az account show --query id -o tsv)
ACR="${ACR:-chdbanalyst$(printf '%s/%s' "${SUB}" "${RG}" | shasum | cut -c1-12)}"
echo "==> deleting registry ${ACR}"
az acr delete -n "${ACR}" -g "${RG}" --yes -o none 2>/dev/null || echo "    (none)"

# delete the group only if deploy.sh created it (its chdb-cookbook tag proves
# ownership) and it's now empty — never destroy a pre-existing group, empty
# or not
OWNED=$(az group show -n "${RG}" --query "tags.\"chdb-cookbook\"" -o tsv 2>/dev/null)
EMPTY=$(az resource list -g "${RG}" --query 'length(@)' -o tsv 2>/dev/null)
if [ "${OWNED}" = "true" ] && [ "${EMPTY}" = "0" ]; then
  echo "==> resource group ${RG} was created by deploy.sh and is empty — deleting it"
  az group delete -n "${RG}" --yes
else
  echo "==> leaving resource group ${RG} (not cookbook-created or not empty)"
fi
echo "done."
