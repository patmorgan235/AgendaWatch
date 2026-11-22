# Agenda Watch — Runbook (Azure)

How the hosted daily run works and how to operate it. See [project-plan.md](project-plan.md) for
why it's built this way.

## What runs where

| Piece | Azure resource (resource group `rg-agendawatch`) |
|---|---|
| Daily run | Container Apps job `agendawatch-daily`, scheduled `0 11 * * *` UTC (5 AM CST / 6 AM CDT) |
| Public site | Storage account static website (`$web` container); URL printed by `infra/deploy.sh` |
| Database | `state/agenda_watch.db` in the same storage account (private), with 14-day soft delete |
| Run lock | `state/run.lock` (blob lease) |
| Image | Azure Container Registry `<prefix>acr…`, repository `agendawatch` |
| Logs | Log Analytics workspace `agendawatch-logs`, table `ContainerAppConsoleLogs_CL` |
| Alert | "Agenda Watch: no successful run in 26 hours" (email), if `ALERT_EMAIL` was set |

Each run: take the lock → download the database → read every source → save the database →
publish the site → release the lock. Exit codes: `0` ok (some sources may have failed), `3` every
source failed, `75` another run held the lock.

## First-time setup

```bash
az login
SUBSCRIPTION="Pay-As-You-Go" ALERT_EMAIL=you@example.com infra/deploy.sh   # resources + static website
SUBSCRIPTION="Pay-As-You-Go" infra/push-image.sh                           # build, push, point the job at it
az containerapp job start -g rg-agendawatch -n agendawatch-daily            # first run now
```

Then set the GitHub Actions variables that `deploy.sh` prints (`AZURE_CLIENT_ID`,
`AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `ACR_NAME`, `JOB_NAME`).
From then on every push to `main` runs the tests, builds the image, and updates the job.

## Everyday tasks

**Run now (outside the schedule)**
```bash
az containerapp job start -g rg-agendawatch -n agendawatch-daily
```

**See recent runs**
```bash
az containerapp job execution list -g rg-agendawatch -n agendawatch-daily \
  --query "[].{name:name,status:properties.status,start:properties.startTime}" -o table
```

**Read a run's log** (Log Analytics → Logs, or `az monitor log-analytics query`):
```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(1d)
| extend e = parse_json(Log_s)
| project TimeGenerated, level = tostring(e.level), message = tostring(e.message), event = tostring(e.event)
| order by TimeGenerated asc
```
Per-source results: add `| where event == "source"` and project `e.source`, `e.listed`, `e.error`.

**Change terms or sources:** edit `config.yaml`, push to `main`. The new image is used from the
next run.

**Change the schedule, site URL, or alert email:** re-run `infra/deploy.sh` with
`SCHEDULE_CRON`, `SITE_URL` or `ALERT_EMAIL` set. It keeps the current image.

## When something goes wrong

**Alert: no successful run in 26 hours**
1. List recent executions (above). A `Failed` run → read its log for the traceback.
2. Exit `75` in the log ("another run is still in progress"): a previous run is stuck or crashed
   holding the lease. It's taken over automatically after 4 hours; to clear it now:
   `az storage blob lease break --account-name <storage> -c state -b run.lock --auth-mode login`.
3. Exit `3` ("every source failed"): a network or DNS problem in Azure. Start a run manually later.

**A source shows an error in the report's Sources table:** expected for Hurst from Azure
(blocked, HTTP 403). Otherwise the city probably changed its site or platform; run
`python agenda_watch.py check` locally and adjust `config.yaml`.

**Roll back to an earlier image**
```bash
az acr repository show-tags -n <registry> --repository agendawatch -o table
az containerapp job update -g rg-agendawatch -n agendawatch-daily --image <registry>.azurecr.io/agendawatch:<tag>
```

**Restore the database** (it has 14 days of soft delete and is overwritten each run): in the
portal, storage account → `state` container → show deleted blobs/versions → undelete; or delete
`agenda_watch.db` to start fresh (everything in the window is reported as new once).

## Costs (rough, per month)

Container Registry Basic ≈ $5; storage, Log Analytics and the job itself are cents (the job fits
in Container Apps' monthly free grant). Delete everything with
`az group delete -n rg-agendawatch`.
