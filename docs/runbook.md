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

## Google Calendar sync (optional)

There are two ways to get meetings into Google Calendar:
- **Subscribe by URL (no setup).** Anyone can do this. Use the site's "Subscribe to a city or section" panel, or in Google Calendar choose **Other calendars → + → From URL** and paste `https://agendas.urbancivic.cloud/feeds/city/<city>.ics`. Google refreshes subscribed calendars every several hours.
- **Push into calendars you own (this section).** After each run, the job writes events into Google Calendars you control, for example one official calendar per town. They update right away, and you can make them public, share them or embed them.

Only meetings whose agendas mention a watched term are pushed, the same set as the site.

**1. Google side (once)**
1. In the [Google Cloud console](https://console.cloud.google.com/), create a project (e.g. `agendawatch`) and enable the **Google Calendar API** for it.
2. Go to **IAM & Admin → Service accounts → Create service account** (e.g. `agendawatch-sync`). It doesn't need any project roles.
3. On the service account, open **Keys → Add key → JSON** and download the key file. Keep it private: it can edit every calendar you share with it.
4. In [Google Calendar](https://calendar.google.com/), create a calendar for each town (**Other calendars → + → Create new calendar**), e.g. "Plano – transit agenda items".
5. For each calendar, open **Settings and sharing → Share with specific people**, add the service account's email (`…@….iam.gserviceaccount.com`), and give it **Make changes to events**.
6. Still in each calendar's settings, copy the **Calendar ID** from **Integrate calendar**. It looks like `abc123@group.calendar.google.com`.
7. Optional: to share a calendar publicly, tick **Make available to public**. The same page has the public URL and the embed code.

**2. Configure**
Add the calendars to `config.yaml`. Calendar IDs aren't secret.
```yaml
google_calendar:
  calendars:
    Plano: abc123@group.calendar.google.com          # by city (as in the site's City filter)
    "DART member cities": def456@group.calendar.google.com   # or by report section
    "*": all789@group.calendar.google.com             # or everything
```

**3. Test locally (optional)**
```bash
export GOOGLE_APPLICATION_CREDENTIALS=~/Downloads/agendawatch-sync-key.json
python agenda_watch.py run              # writes reports/data.json
python agenda_watch.py gcal --dry-run   # lists what would be added, updated or removed
python agenda_watch.py gcal             # does it; run it again and everything is "unchanged"
```

**4. Turn it on in Azure.** The key lives in Key Vault, so redeploys never remove it.
```bash
infra/deploy.sh   # creates the Key Vault and lets you (the signed-in user) store secrets in it
VAULT=$(az keyvault list -g rg-agendawatch --query "[0].name" -o tsv)
az keyvault secret set --vault-name "$VAULT" -n google-credentials --file ~/Downloads/agendawatch-sync-key.json -o none
GOOGLE_CALENDAR=true infra/deploy.sh   # gives the job the secret; later runs of deploy.sh keep it on
```
Then delete the downloaded key file. Commit the `config.yaml` change; the next push deploys it.

**Check:** after the next run, Log Analytics shows a `gcal_synced` event with the counts:
```kusto
ContainerAppConsoleLogs_CL | where Log_s has "gcal_" | project TimeGenerated, Log_s | order by TimeGenerated desc
```

**Troubleshooting**
- **`gcal_failed` … "is the calendar shared with the service account…"**: step 5 is missing for that calendar, or its ID is wrong. The other calendars still sync.
- **`no credentials set; skipped`**: `GOOGLE_CALENDAR=true` wasn't deployed, or the secret is missing.
- **Rotate the key:** run `az keyvault secret set` again with the new key. The job reads it at its next start, and you can then delete the old key in Google Cloud.
- **Turn it off:** run `GOOGLE_CALENDAR=false infra/deploy.sh`, or remove `google_calendar` from `config.yaml`.

A calendar problem never fails the run: the site is already published by then. Events are only ever added, updated or removed if this tool created them, and events before the report window are kept as history.

## Costs (rough, per month)

Container Registry Basic ≈ $5; storage, Log Analytics and the job itself are cents (the job fits
in Container Apps' monthly free grant). Key Vault and the Google Calendar API are free at this volume. Delete everything with
`az group delete -n rg-agendawatch`.
