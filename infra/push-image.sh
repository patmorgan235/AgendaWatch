#!/usr/bin/env bash
# Build the image locally, push it to the registry, and point the job at it.
# Used for the first deploy (and whenever GitHub Actions isn't available).
#
#   SUBSCRIPTION="Pay-As-You-Go" RESOURCE_GROUP=rg-agendawatch infra/push-image.sh
#
# Uses podman if present, otherwise docker. Building here (not in Azure) avoids Docker Hub's
# rate limits on cloud IPs for the python base image.
set -euo pipefail

: "${SUBSCRIPTION:?set SUBSCRIPTION (name or id)}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-agendawatch}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="$(command -v podman || command -v docker)"

az account set --subscription "$SUBSCRIPTION"
ACR="$(az acr list -g "$RESOURCE_GROUP" --query "[0].name" -o tsv)"
SERVER="$(az acr show -n "$ACR" --query loginServer -o tsv)"
JOB="$(az containerapp job list -g "$RESOURCE_GROUP" --query "[?ends_with(name,'-daily')].name | [0]" -o tsv)"
JOB_ID="$(az identity show -g "$RESOURCE_GROUP" -n "${JOB%-daily}-job-id" --query id -o tsv)"
TAG="$(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet || echo -dirty)"
IMAGE="$SERVER/agendawatch:$TAG"

"$ENGINE" build -t "$IMAGE" -t "$SERVER/agendawatch:latest" "$ROOT"
az acr login -n "$ACR" --expose-token --query accessToken -o tsv \
  | "$ENGINE" login "$SERVER" -u 00000000-0000-0000-0000-000000000000 --password-stdin
"$ENGINE" push "$IMAGE"
"$ENGINE" push "$SERVER/agendawatch:latest"

# Pull with the job's managed identity (idempotent), then switch the image.
az containerapp job registry set -g "$RESOURCE_GROUP" -n "$JOB" --server "$SERVER" --identity "$JOB_ID" -o none
az containerapp job update -g "$RESOURCE_GROUP" -n "$JOB" --image "$IMAGE" -o none
echo "Job $JOB now runs $IMAGE"
