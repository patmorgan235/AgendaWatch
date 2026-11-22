#!/usr/bin/env bash
# Deploy (or update) the Agenda Watch Azure resources.
#
#   SUBSCRIPTION="Pay-As-You-Go" RESOURCE_GROUP=rg-agendawatch LOCATION=southcentralus \
#   ALERT_EMAIL=you@example.com infra/deploy.sh
#
# Safe to re-run: it keeps the image the job currently runs, so a redeploy never reverts it.
# Optional: SCHEDULE_CRON (UTC, default "0 11 * * *"), SITE_URL, GITHUB_REPO, IMAGE.
set -euo pipefail

: "${SUBSCRIPTION:?set SUBSCRIPTION (name or id)}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-agendawatch}"
LOCATION="${LOCATION:-southcentralus}"
ALERT_EMAIL="${ALERT_EMAIL:-}"
SCHEDULE_CRON="${SCHEDULE_CRON:-0 11 * * *}"
SITE_URL="${SITE_URL:-}"
GITHUB_REPO="${GITHUB_REPO:-patmorgan235/AgendaWatch}"
HERE="$(cd "$(dirname "$0")" && pwd)"

az account set --subscription "$SUBSCRIPTION"

# Resource providers the template needs (Microsoft.App is often unregistered on older subscriptions).
for ns in Microsoft.App Microsoft.ContainerRegistry Microsoft.ManagedIdentity Microsoft.OperationalInsights Microsoft.Insights Microsoft.Storage; do
  if [ "$(az provider show -n "$ns" --query registrationState -o tsv)" != "Registered" ]; then
    echo "Registering $ns (one-time, can take a few minutes)…"
    az provider register -n "$ns" --wait
  fi
done

az group create -n "$RESOURCE_GROUP" -l "$LOCATION" --tags app=agendawatch -o none

# GitHub's numeric owner/repo IDs, for the immutable-ID OIDC subject (needs the gh CLI).
GITHUB_OWNER_ID="${GITHUB_OWNER_ID:-}"; GITHUB_REPO_ID="${GITHUB_REPO_ID:-}"
if [ -z "$GITHUB_REPO_ID" ] && command -v gh >/dev/null; then
  read -r GITHUB_OWNER_ID GITHUB_REPO_ID < <(gh api "repos/$GITHUB_REPO" --jq '"\(.owner.id) \(.id)"' 2>/dev/null || echo "")
fi
[ -z "$GITHUB_REPO_ID" ] && echo "Note: couldn't look up GitHub repo IDs; only the classic OIDC subject is registered."

# Keep the current image if the job already exists.
IMAGE="${IMAGE:-$(az containerapp job list -g "$RESOURCE_GROUP" --query "[?ends_with(name,'-daily')].properties.template.containers[0].image | [0]" -o tsv 2>/dev/null || true)}"
image_args=()
[ -n "$IMAGE" ] && image_args=(image="$IMAGE")
echo "Deploying to $RESOURCE_GROUP ($LOCATION); image: ${IMAGE:-placeholder}"

out="$(az deployment group create -g "$RESOURCE_GROUP" -n agendawatch -f "$HERE/main.bicep" \
  -p alertEmail="$ALERT_EMAIL" scheduleCron="$SCHEDULE_CRON" siteUrl="$SITE_URL" githubRepo="$GITHUB_REPO" \
     githubOwnerId="$GITHUB_OWNER_ID" githubRepoId="$GITHUB_REPO_ID" \
  "${image_args[@]}" --query properties.outputs -o json)"

get() { echo "$out" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1']['value'])"; }
STORAGE="$(get storageAccount)"

# The static website is a data-plane setting Bicep can't turn on.
az storage blob service-properties update --account-name "$STORAGE" --auth-mode login \
  --static-website --index-document index.html --404-document index.html -o none 2>/dev/null \
|| az storage blob service-properties update --account-name "$STORAGE" \
  --static-website --index-document index.html --404-document index.html -o none

cat <<EOF

Deployed.
  Site:               $(get siteUrl)
  Storage account:    $STORAGE
  Registry:           $(get registryLoginServer)
  Job:                $(get jobName)
  Log Analytics:      $(get logAnalyticsWorkspace)

GitHub Actions variables (repo Settings → Secrets and variables → Actions → Variables):
  AZURE_CLIENT_ID        $(get deployIdentityClientId)
  AZURE_TENANT_ID        $(az account show --query tenantId -o tsv)
  AZURE_SUBSCRIPTION_ID  $(az account show --query id -o tsv)
  AZURE_RESOURCE_GROUP   $RESOURCE_GROUP
  ACR_NAME               $(get registryName)
  JOB_NAME               $(get jobName)

First image: infra/push-image.sh  (later pushes to main deploy through GitHub Actions)
Run now:     az containerapp job start -g $RESOURCE_GROUP -n $(get jobName)
EOF
