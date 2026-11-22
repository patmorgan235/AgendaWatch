# Agenda Watch — Runbook (Azure)

How the hosted daily run works and how to operate it. See [project-plan.md](project-plan.md) for
why it's built this way.

## What runs where

| Piece | Azure resource (resource group `rg-agendawatch`) |
|---|---|
| Daily run | Container Apps job `agendawatch-daily`, scheduled `0 11 * * *` UTC (5 AM CST / 6 AM CDT); the container runs on Central time (`TZ=America/Chicago`) |
| Public site | https://agendas.urbancivic.cloud (through Cloudflare) → storage account `agendawatchbcfoiznunxe4`, static website (`$web` container): https://agendawatchbcfoiznunxe4.z21.web.core.windows.net/ |
| Database | `state/agenda_watch.db` in the same storage account (private), with 14-day soft delete |
| Run lock | `state/run.lock` (blob lease) |
| Image | Azure Container Registry `agendawatchacrbcfoiznunx`, repository `agendawatch` |
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

## Custom domain (agendas.urbancivic.cloud via Cloudflare)

Azure Storage's static website can't serve HTTPS on a custom domain itself, so Cloudflare's free
proxy provides the certificate:

```
browser ──HTTPS──▶ Cloudflare (certificate for agendas.urbancivic.cloud)
                      └─HTTPS──▶ agendawatchbcfoiznunxe4.z21.web.core.windows.net
```

The storage account also registers the domain, so it answers requests for that hostname.
Cloudflare's free plan can't rewrite the Host header, which is why the domain is registered on the
account instead. Registration uses Azure's *indirect* `asverify` check, because Cloudflare's proxy
would break the direct check.

**One-time setup**

1. **Cloudflare:** create a free account, choose *Add a site*, enter `urbancivic.cloud`, and pick the Free plan.
2. **Registrar:** replace the nameservers for `urbancivic.cloud` with the two Cloudflare shows.
   Cloudflare emails when the zone is active, usually within an hour and sometimes up to 24 hours.
3. **Cloudflare → DNS → Records:** add

   | Type | Name | Target | Proxy |
   |---|---|---|---|
   | CNAME | `asverify.agendas` | `asverify.agendawatchbcfoiznunxe4.blob.core.windows.net` | **DNS only** (grey cloud) |
   | CNAME | `agendas` | `agendawatchbcfoiznunxe4.z21.web.core.windows.net` | **Proxied** (orange cloud) |

4. **Cloudflare → SSL/TLS → Overview:** set the encryption mode to **Full**, not "Full (strict)":
   Azure presents its own `*.web.core.windows.net` certificate. Under **Edge Certificates**, turn
   on **Always Use HTTPS**.
5. **Azure:** register the domain and switch the feed links to it:
   ```bash
   SUBSCRIPTION="Pay-As-You-Go" ALERT_EMAIL=you@example.com \
   CUSTOM_DOMAIN=agendas.urbancivic.cloud infra/deploy.sh
   az containerapp job start -g rg-agendawatch -n agendawatch-daily   # republish with the new URL
   ```
   Later runs of `deploy.sh` keep the domain automatically.

**Check:** `curl -sI https://agendas.urbancivic.cloud/` should return `200`, with `server: cloudflare`.
The original `*.web.core.windows.net` address keeps working too.

**Troubleshooting**
- **`deploy.sh` fails with a custom-domain validation error:** the `asverify.agendas` record is missing, still propagating, or proxied. It must be DNS only.
- **Cloudflare error 526:** SSL mode is "Full (strict)". **Error 525 or redirect loops:** SSL mode is "Flexible". Use "Full".
- **HTTP 400 "InvalidUri" or similar from Azure:** the domain isn't registered on the storage account yet (step 5).

## Costs (rough, per month)

Container Registry Basic ≈ $5; storage, Log Analytics and the job itself are cents (the job fits
in Container Apps' monthly free grant). Delete everything with
`az group delete -n rg-agendawatch`.
