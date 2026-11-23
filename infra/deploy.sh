#!/usr/bin/env bash
# Deploy (or update) the Agenda Watch Azure resources.
#
#   SUBSCRIPTION="Pay-As-You-Go" RESOURCE_GROUP=rg-agendawatch LOCATION=southcentralus \
#   ALERT_EMAIL=you@example.com infra/deploy.sh
#
# Safe to re-run: it keeps the image the job currently runs, so a redeploy never reverts it.
# Optional: SCHEDULE_CRON (UTC, default "0 11 * * *"), SITE_URL, GITHUB_REPO, IMAGE,
#           CUSTOM_DOMAIN (e.g. agendas.urbancivic.cloud; see docs/runbook.md "Custom domain"),
#           GOOGLE_CALENDAR=true|false (push to Google Calendars; see docs/runbook.md "Google Calendar sync").
set -euo pipefail

: "${SUBSCRIPTION:?set SUBSCRIPTION (name or id)}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-agendawatch}"
LOCATION="${LOCATION:-southcentralus}"
ALERT_EMAIL="${ALERT_EMAIL:-}"
SCHEDULE_CRON="${SCHEDULE_CRON:-0 11 * * *}"
SITE_URL="${SITE_URL:-}"
CUSTOM_DOMAIN="${CUSTOM_DOMAIN:-}"
GITHUB_REPO="${GITHUB_REPO:-patmorgan235/AgendaWatch}"
HERE="$(cd "$(dirname "$0")" && pwd)"

az account set --subscription "$SUBSCRIPTION"

# Resource providers the template needs (Microsoft.App is often unregistered on older subscriptions).
for ns in Microsoft.App Microsoft.ContainerRegistry Microsoft.ManagedIdentity Microsoft.OperationalInsights Microsoft.Insights Microsoft.Storage Microsoft.KeyVault; do
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

# Keep the storage account's current custom domain unless CUSTOM_DOMAIN is given.
if [ -z "$CUSTOM_DOMAIN" ]; then
  CUSTOM_DOMAIN="$(az storage account list -g "$RESOURCE_GROUP" --query "[0].customDomain.name" -o tsv 2>/dev/null || true)"
fi

# Keep the current image if the job already exists.
IMAGE="${IMAGE:-$(az containerapp job list -g "$RESOURCE_GROUP" --query "[?ends_with(name,'-daily')].properties.template.containers[0].image | [0]" -o tsv 2>/dev/null || true)}"
image_args=()
[ -n "$IMAGE" ] && image_args=(image="$IMAGE")

# Keep Google Calendar sync on if the job already has it, unless GOOGLE_CALENDAR says otherwise.
if [ -z "${GOOGLE_CALENDAR:-}" ]; then
  has_gcal="$(az containerapp job list -g "$RESOURCE_GROUP" \
    --query "[?ends_with(name,'-daily')].properties.template.containers[0].env[] | [?name=='AGENDAWATCH_GOOGLE_CREDENTIALS'] | length(@)" \
    -o tsv 2>/dev/null || true)"
  if [ "${has_gcal:-0}" = "0" ] || [ -z "$has_gcal" ]; then GOOGLE_CALENDAR=false; else GOOGLE_CALENDAR=true; fi
fi
# Let the person deploying store the Google key in the Key Vault (empty when signed in as a service principal).
SECRETS_OFFICER="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"

echo "Deploying to $RESOURCE_GROUP ($LOCATION); image: ${IMAGE:-placeholder}; custom domain: ${CUSTOM_DOMAIN:-none}; Google Calendar: $GOOGLE_CALENDAR"

out="$(az deployment group create -g "$RESOURCE_GROUP" -n agendawatch -f "$HERE/main.bicep" \
  -p alertEmail="$ALERT_EMAIL" scheduleCron="$SCHEDULE_CRON" siteUrl="$SITE_URL" githubRepo="$GITHUB_REPO" \
     githubOwnerId="$GITHUB_OWNER_ID" githubRepoId="$GITHUB_REPO_ID" customDomain="$CUSTOM_DOMAIN" \
     googleCalendar="$GOOGLE_CALENDAR" secretsOfficerPrincipalId="$SECRETS_OFFICER" \
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
  Storage website:    https://$(get webEndpointHost)/
  Storage account:    $STORAGE
  Registry:           $(get registryLoginServer)
  Job:                $(get jobName)
  Log Analytics:      $(get logAnalyticsWorkspace)
  Key Vault:          $(get keyVaultName)   (Google Calendar sync: $GOOGLE_CALENDAR)

GitHub Actions variables (repo Settings → Secrets and variables → Actions → Variables):
  AZURE_CLIENT_ID        $(get deployIdentityClientId)
  AZURE_TENANT_ID        $(az account show --query tenantId -o tsv)
  AZURE_SUBSCRIPTION_ID  $(az account show --query id -o tsv)
  AZURE_RESOURCE_GROUP   $RESOURCE_GROUP
  ACR_NAME               $(get registryName)
  JOB_NAME               $(get jobName)

Custom domain DNS (Cloudflare):
  asverify.<domain>  CNAME  $(get asverifyTarget)   (DNS only — not proxied)
  <domain>           CNAME  $(get webEndpointHost)   (proxied; SSL/TLS mode "Full")

First image: infra/push-image.sh  (later pushes to main deploy through GitHub Actions)
Run now:     az containerapp job start -g $RESOURCE_GROUP -n $(get jobName)
EOF
