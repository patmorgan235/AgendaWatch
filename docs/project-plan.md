# AgendaWatch — Project Plan

_Last updated: November 22, 2026 (Phases 0–4 complete; next: Phase 5, tags & terms)_

## Goal

Tell North Texas transit advocates about transit-related items going before local government
bodies **before the meetings happen**, early enough to read the item, organize, and comment or
speak.

A searchable history of past agenda items is useful but secondary.

## Audience

- **Primary:** transit advocates.
- The site is **public**.
- The project owner maintains the term list and the source configuration.

## Where we are (November 22, 2026)

- **Live:** https://agendas.urbancivic.cloud (also https://agendawatchbcfoiznunxe4.z21.web.core.windows.net/)
  - Updated daily at 11:00 UTC (5 AM CST / 6 AM CDT) by an Azure Container Apps job.
  - The site has filters (upcoming only, new since your last visit, section, city, term, search), an Atom feed (`feed.xml`), a calendar feed (`upcoming.ics`) and the raw data (`data.json`).
- **Coverage:** 43 sources (42 bodies; Richardson has two sources). Report sections: DART member cities, DCTA member cities, Trinity Metro member & associate cities, Regional (RTC), Counties, and Eligible to join a transit agency.
- **Platforms supported:**
  - Legistar, CivicClerk, CivicWeb;
  - CivicPlus Agenda Center and Archive Center;
  - destinyhosted/eAgenda, Granicus ViewPublisher, Swagit;
  - Docs Published (Fort Worth), OnBase Agenda Online (Frisco);
  - generic link pages and document tables.
- **Reading:** scanned agendas are read with OCR. Each passage is reported once per meeting, so an amended agenda only surfaces genuinely new text.
- **Delivery:** pushes to `main` run the tests and deploy automatically through GitHub Actions. An email alert fires if a day passes without a successful run.
- **Local use:** the tool still runs locally (`agenda_watch.py run`) and writes the same site into `reports/`.

### Known gaps

| Gap | Notes |
|---|---|
| Transit agency boards not watched | DART, Trinity Metro and DCTA boards, plus NCTCOG's Surface Transportation Technical Committee (STTC), which vets items before the RTC. |
| Blocked sources | Irving, Tarrant County, Grand Prairie, The Colony, Burleson, Prosper, Allen and Saginaw sit behind Cloudflare challenges, Municode Meetings, or token-only AgendaLink APIs. |
| Blocked from the cloud only | Richardson's cor.net (Swagit fallback gives shorter notice) and Hurst (no fallback) return 403 to Azure addresses; see the hybrid fetch in the backlog. |
| Unresolved sources | Arlington (its agenda list is loaded by script), Rockwall and Corinth (sites unreachable). |
| Titles only | Fort Worth, Dallas County's HTML agenda, and Dallas Legistar with attachments off give item titles but not staff reports. |
| ~~Report hides earlier finds~~ | Fixed in Phase 1: the page shows every match for meetings in the window. |
| Limited push delivery | The RSS/Atom and calendar feeds exist (Phase 1), but there are no email or Slack alerts for new matches (Later). |
| ~~No lead-time measurement~~ | Phase 4: the "Notice" column shows the median days of notice per source. |
| ~~Silent source drift~~ | Phase 4: the stale-source check flags a source with no new agenda for 30+ days. |

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Hosting model | **Option B:** a scheduled container job plus a static site | More control than GitHub Actions (memory for large PDFs, OCR tools in the image, predictable start times). |
| Cloud | **Azure** (owner's most familiar platform) | Storage is kept behind an interface so it can move to S3 or R2 later. |
| Report delivery | Static site built from a published JSON data file | Filtering happens in the browser; RSS and calendar feeds come from the same data; fixes the hidden-earlier-finds gap. |
| Term list | Maintained by the owner in `config.yaml` | No admin interface needed. |
| Change workflow | Every change goes on a branch and through a GitHub pull request; merging to `main` deploys | A second collaborator joined on Nov 22, 2026. Tests run on every pull request. |
| Sources that block cloud IPs | Fetched from the owner's home machine (the "hybrid fetch") and handed to the hosted run | Approved Nov 22, 2026. Restores full notice for Richardson (cor.net) and Hurst. |
| Irving | A real headless browser on the home machine loads the agenda page, passing Cloudflare's check as a normal browser does | Approved Nov 22, 2026. No challenge-solving code, and nothing runs from the cloud. If Irving's PrimeGov portal goes live, use its API instead. |

## Target architecture (Azure)

```
            ┌──────────────────────────────┐
  cron ───▶ │ Container Apps Job           │  managed identity
            │  1. take lease, download DB  │──────────────┐
            │  2. agenda_watch run         │              ▼
            │  3. write site + data.json   │      ┌─────────────────────────┐
            │  4. upload DB, release lease │─────▶│ Storage account          │
            └──────────────────────────────┘      │  $web  (public site)     │
                                                  │  state (private: SQLite) │
                                                  └───────────┬─────────────┘
                                                              │ optional Front Door / CDN
                                                              ▼
                                                       advocates' browsers,
                                                       RSS readers, calendars
```

| Piece | Service | Notes |
|---|---|---|
| Scheduled job | Azure Container Apps Jobs (schedule trigger) | Daily; per-run time limit about 2 hours; billed per second. |
| Public site | Blob Storage static website (`$web`) | Optional Front Door or CDN for a custom domain with HTTPS. |
| State | Private blob container holding `agenda_watch.db` | A blob lease prevents overlapping runs. |
| Credentials | Managed identity | No keys in the container. |
| Image | GitHub Container Registry or Azure Container Registry | Built by GitHub Actions on push to `main`. |
| Infrastructure | Bicep templates in the repo | |
| Monitoring | Log Analytics plus an alert on a failed run | |

## Phases

### Phase 0 — Cloud reachability test ✅ done Nov 22, 2026

Government sites sometimes block data-center IP addresses. This could change the hosting plan,
so it came first.

- [x] Build a throwaway container and run the source check once from Azure Container Instances (Pay-As-You-Go subscription, South Central US).
- [x] Compare the results source by source with a run from a home connection.
- [x] Decide: all of it on Azure, or a self-hosted runner for the scraping with the site still on Azure.

**Results** (42 sources; each test listed the source's agendas and downloaded its first one):

| | Home connection | Azure, South Central US |
|---|---|---|
| Sources listed | 42 / 42 | 40 / 42 |
| First downloads OK (where an agenda was in the window) | 37 / 37 | 35 / 35 |
| Blocked | — | **Richardson** (cor.net) and **Hurst** (hursttx.gov): HTTP 403 from Akamai bot filtering; the browser-header mode doesn't help from a data-center address |

**Decision:** host everything on Azure.

- **Richardson:** the Swagit video archive is re-enabled as a fallback. It lists meetings around meeting day, so notice is shorter, but it also covers boards that cor.net's council page doesn't (for example, the Bicycle and Pedestrian Advisory Committee). Swagit wasn't part of the Azure test; confirm it on the first Azure run.
- **Hurst:** no cloud fallback found. Its Swagit listing stops in 2014, and the current listing is on the blocked city site. It shows as an error in the Sources table until the hybrid job (backlog) exists.
- **Asking the cities** to allowlist a User-Agent or IP: not pursued, as they're unlikely to change IT settings for one person.

**Lessons for Phase 2:**
- Anonymous Docker Hub pulls from Azure hit rate limits, so use Microsoft Container Registry base images or our own registry.
- The test cost a few cents, and its resource group was deleted afterwards.

### Phase 1 — Report data and static site (local, no cloud) ✅ done Nov 22, 2026

- [x] **`data.json` export:**
  - every match for meetings in the report window, with `first_seen`, section, city, body, meeting date, term, snippet and links;
  - upcoming meetings with matches;
  - per-source status.
- [x] **Static page** that renders from `data.json`:
  - filter to upcoming meetings only;
  - filter by section, city and term;
  - "new since your last visit" (stored in the browser), defaulting to "new since the last run";
  - sort by meeting date and show the days remaining ("in 6 days");
  - mobile-friendly layout;
  - shareable links to a single meeting.
- [x] **RSS/Atom feed** of new matches.
- [x] **Calendar (.ics) feed** of upcoming meetings that mention a term.
- [x] **Footer:** a link to each official posting, the "confirm against the official agenda" disclaimer, and the time of the last run.
- [x] Keep the existing single-file HTML report working for local use.

**Done when:** running the tool twice in a row still shows every in-window match; the upcoming-only filter works; the feeds validate.

### Phase 2 — Packaging ✅ done Nov 22, 2026

- [x] `Dockerfile` (python:3.12-slim, non-root user, `ocrmypdf` for scanned PDFs on by default since Phase 3; `--build-arg WITH_OCR=false` for a 200 MB image without it). The default command is `job`.
- [x] Storage interface (`agendawatch/storage.py`) with `local`, `azure` (Blob, managed identity) and `s3` (any S3-compatible) backends, selected by `AGENDAWATCH_STORAGE`.
- [x] Run lock: an Azure blob lease, an S3 conditional put, or a local exclusive file. A lock older than `AGENDAWATCH_LOCK_TTL_HOURS` (default 4) is taken over.
- [x] Every `settings:` value can be overridden with `AGENDAWATCH_SETTING_<NAME>`; the config path with `AGENDAWATCH_CONFIG`.
- [x] Database growth: `page_retention_days` drops page text for older meetings and compacts the file; matches are always kept.
- [x] JSON log lines (`AGENDAWATCH_LOG_FORMAT=json`, the image default), with one summary line per source and a run summary.
- [x] Exit codes:
  - `0` — success, even when some sources fail; the Hurst 403 from Azure shouldn't page anyone daily.
  - `3` — every source failed.
  - `75` — the run lock is held.
  - `1` — unexpected error.

**Verified:**
- The image built with Podman and ran `job` twice against three live sources (Plano on Legistar, Rowlett on CivicClerk, Fort Worth on Docs Published) with local storage.
- Run 1 started a fresh database and published the site (30 seconds).
- Run 2 reused the stored database, found nothing new, and still published the same meetings, marked not new (18 seconds).
- Feed links use `site_url`.
- The Azure and S3 backends are tested only against fake clients so far. The Azure backend gets its first real test in Phase 3.

### Phase 3 — Azure deployment ✅ deployed Nov 22, 2026

- [x] Bicep templates (`infra/main.bicep`):
  - a storage account with the static website and a private `state` container (14-day soft delete);
  - a Container Registry (Basic);
  - Log Analytics;
  - a Container Apps environment and the scheduled job (daily at 11:00 UTC, 2 vCPU / 4 GiB, 2-hour timeout);
  - a job identity (storage read/write, image pull) and a deploy identity (image push, job update) with a GitHub OIDC federated credential;
  - an email alert when there has been no successful run in 26 hours.
- [x] `infra/deploy.sh` (registers providers, deploys, keeps the job's current image, turns on the static website) and `infra/push-image.sh` (local build and push).
- [x] GitHub Actions (`.github/workflows/deploy.yml`): tests on every push and pull request; on `main`, builds, pushes and updates the job. The repo variables are set.
- [x] Runbook: `docs/runbook.md`.
- [x] Custom domain **https://agendas.urbancivic.cloud** (live Nov 22).
  - Cloudflare provides DNS and HTTPS: free plan, proxied CNAME, SSL mode "Full".
  - The storage account registers the domain with indirect `asverify` validation, because Cloudflare's free plan can't rewrite the Host header. Front Door (≈$35/month) wasn't needed.
  - `deploy.sh` keeps the domain on later runs.
  - Verified: the page, `data.json`, `feed.xml` and `upcoming.ics` are served over HTTPS with a valid certificate (`server: cloudflare`), and the feed and calendar links use the domain.
  - Remaining: turn on Cloudflare's "Always Use HTTPS". Until then, plain `http://` gets HTTP 400 from Azure.

**Deployed:** Pay-As-You-Go subscription, resource group `rg-agendawatch`, South Central US.
- Site: https://agendas.urbancivic.cloud (storage endpoint: https://agendawatchbcfoiznunxe4.z21.web.core.windows.net/)
- Registry: `agendawatchacrbcfoiznunx`
- Job: `agendawatch-daily`

**First hosted run (Nov 22):**
- Succeeded in 12 minutes: 41 of 43 sources OK, 42 new matches, an 11.8 MB database.
- Only cor.net (Richardson) and Hurst failed, with HTTP 403, as Phase 0 predicted.
- The Richardson Swagit fallback works from Azure.
- The site serves all files with the right content types.

**Found and fixed after the first run:**
- **Wrong times:** the container ran on UTC, so the "updated" time showed 10:06 PM for a 4:06 PM run. The image and the job now set `TZ=America/Chicago`. Verified: the next run published `generated 16:39` for a run that ended at 22:39 UTC.
- **Unregistered provider:** `Microsoft.App` wasn't registered on the subscription. `deploy.sh` now registers missing providers; the what-if preview had missed this.
- **No OCR:** the image had no OCR, so scanned agendas weren't read. OCR (`ocrmypdf`) is now built in by default, and the image grows from 197 MB to 446 MB. Tested on Richardson's scanned Bicycle and Pedestrian Advisory Committee agenda: 0 → 2,573 characters, in 3 seconds. Agendas already read without OCR are re-read only if their file changes.
- **GitHub sign-in failed:** the first automatic deploy couldn't sign in to Azure. GitHub issued its token with the immutable-ID subject `repo:patmorgan235@2245348/AgendaWatch@1467803098:ref:refs/heads/main`, but only the classic `repo:patmorgan235/AgendaWatch:…` form was registered. The template now registers both, and `deploy.sh` looks up the IDs with `gh`. Verified: the next push (`c77d417`) tested, built and pushed the image, and updated the job automatically.

**Current state:**
- The job runs image `agendawatch:c77d417`.
- An incremental run, which reuses the saved database, takes about 2 minutes. A run starting with no database took 12 minutes.

### Phase 4 — Operations and reliability ✅ done Nov 22, 2026

- [x] An alert when a run fails. Done in Phase 3: an email fires when no run has logged a successful `run_finished` event in 26 hours, which covers crashes, hangs, a stuck lock, every source failing, and the job not starting.
- [x] **Source-health history:** a `source_runs` table records every source's results (listed, read, unchanged, skipped, failed, new, error) for every run. The report shows a failure streak such as "Failing 3 runs in a row, since …"; three or more failures log a `failing_source` warning.
- [x] **Stale-source check:**
  - A source with no new agenda for more than `stale_after_days` (default 30; per-source `stale_after_days` overrides) is flagged "looks stale" in the Sources table and logged as a `stale_source` warning.
  - Sources watched for less than that window aren't judged.
  - A source that errors is reported as failing instead.
- [x] **Lead time per source:** the Sources table's "Notice" column shows the median days between a meeting's agenda first appearing and the meeting, over meetings first seen in the last `lead_window_days` (180). It's measured per meeting, so an agenda and a later packet count once, and the first-day backfill is excluded. It's shown once at least 3 meetings have been measured.
- [x] `index.html#sources` opens the table directly, and the footer has a "source health" link.

**Notes:**
- The health figures fill in over the coming weeks. Lead time needs agendas first seen after a source's first day, and staleness needs 30 or more days of history.
- Hurst will show as failing every run from Azure, as expected (Phase 0).
- **Possible follow-up:** a Log Analytics alert on `stale_source` events, so a quiet source emails you instead of waiting for someone to open the Sources table.

### Phase 5 — Tags and terms (next)

Today there are 11 flat terms, and "Transit" alone is 42% of mentions (77 of 183 after the Nov 22 reset).

- [ ] **Tags:** each term in `config.yaml` gets one or more tags, in three groups:
  - **Agency:** DART, DCTA, Trinity Metro, RTC/NCTCOG.
  - **Mode:** rail, bus, streetcar, paratransit, on-demand.
  - **Topic:** funding (GMP, General Mobility Program), DART membership and withdrawal, transit-oriented development.

  Matches carry their tags into `data.json`, the page, the feed and the calendar.
- [ ] **New terms**, each with tags. Candidates: Silver Line, TEXRail, TRE, light rail, bus rapid transit, GoLink / microtransit / on-demand transit, park-and-ride, transit-oriented development, withdrawal election. **Open:** the owner picks the final list.
- [ ] **Action tags** from the text around each match: interlocal agreement, contract or award, resolution or ordinance, public hearing, presentation or briefing. This separates "the council votes on a DART agreement" from "DART appears in a list of agencies".
- [ ] **Noise rules:** after about a week of daily runs, add `exclude_if_nearby` rules for recurring boilerplate, such as Garland's list of agencies, which includes "(3) Dallas Area Rapid Transit Authority".
- [ ] Re-tag existing matches when tags change, rather than re-downloading agendas.

**Done when:** every match shows its tags; the page filters by tag and action type; and the share of "Transit"-only passing mentions has dropped measurably on real data.

### Phase 6 — Coverage: Richardson and Irving

- [ ] **Richardson, a source that needs no workaround:** look for an earlier posting cor.net doesn't block from the cloud before building anything. Candidates: the "Agendas, Notices & Actions" page, a CivicAlerts or RSS feed, a Granicus or Swagit "upcoming" view.
- [ ] **Hybrid fetch** for sources that block cloud IPs (cor.net for Richardson, hursttx.gov for Hurst):
  - **At home:** a scheduled task on the owner's machine (cron or systemd timer) lists those sources, downloads new or changed agendas, and uploads the files plus a small manifest to an `inbox` container in the storage account. It authenticates with a credential that can only write to `inbox`.
  - **In Azure:** the hosted run reads the inbox through a new `inbox` source type, using the original agenda URLs as links. The database keeps a single writer.
  - Sources marked `fetch: home` are skipped by the cloud run, so they don't show daily 403s. Their health shows when the inbox was last filled, and a stale inbox is flagged.
- [ ] **Irving:**
  - A headless Chrome (Playwright) script on the home machine loads irvingtx.gov's agenda page as a normal browser would, collects the agenda links, and downloads the PDFs into the same inbox.
  - The script runs at the same polite daily rate; it doesn't solve or skip Cloudflare's check in code.
  - If the challenge escalates to something only a person can complete, Irving is reported as unavailable rather than worked around.
- [ ] **Irving PrimeGov reader:** build it now, switched off, plus a check that reports when the portal has real meetings; then switch from the browser fetch to the API.
- [ ] Update the runbook: setting up the home fetcher, its credential, and what happens when the machine is off. Sources fall back or go stale, and the stale-source check flags it.

**Done when:** Richardson's council agendas appear about 6 days ahead on the public site again; Irving's council agendas are covered; and turning the home machine off for a day degrades gracefully and visibly.

### Phase 7 — Search and filtering

- [ ] **Full-text search** of agenda text, not just snippets. Publish a compact search index of the text of meetings in the report window, and search it in the browser, so terms that aren't on the watch list can be found ("bus stop on Belt Line").
- [ ] **Filters:**
  - multi-select for cities and sections;
  - tags and action type (from Phase 5);
  - a date range instead of only upcoming or all;
  - meeting body type (council, planning commission, committees and boards);
  - "has an action item".
- [ ] **Sorting:** by meeting date, by number of mentions, or by newest found.
- [ ] **Named presets** on top of the shareable URL filters, for example "DART cities, action items, next 2 weeks".
- [ ] Keep `data.json` and the page fast on a phone as the data grows: lazy-load the search index, and cap snippets per meeting.

**Done when:** a word not on the term list can be found in the current window's agendas, and the filters above work together and survive a shared link.

### Phase 8 — Deferred coverage

- [ ] **Transit agency boards** (deferred Nov 22, 2026): the DART Board and its committees, the Trinity Metro Board, the DCTA Board, and NCTCOG's Surface Transportation Technical Committee.
- [ ] Revisit the remaining blocked and unresolved sources: Arlington, Rockwall, Corinth, Tarrant County, and the Municode Meetings and AgendaLink cities. The hybrid fetch from Phase 6 may cover some of them.
- [ ] Read deeper where possible: Legistar attachments for key bodies (Dallas stays on agenda PDFs only for now, by choice), and Fort Worth staff reports if a sanctioned way to read them appears.

### Later

- Searchable history beyond the 30-day report window: a static search page or a published read-only copy of the database.
- Email or Slack alerts for new matches, and an email alert for stale sources.

## How we work

- **Branches and pull requests:** every change goes on a branch and through a pull request. GitHub Actions runs the tests on each pull request, and merging to `main` builds the image and deploys it to Azure.
- **Branch protection (suggested, not yet on):** require a pull request and passing tests before merging to `main`, and optionally a review from the collaborator.

## Risks and open questions

| Risk or question | Mitigation or next step |
|---|---|
| Data-center IP addresses get blocked by city sites | Phase 0: only Richardson (cor.net) and Hurst were blocked. Richardson falls back to Swagit for now; the hybrid fetch (Phase 6) restores full notice. Re-check when adding sources. |
| Browser mode for cor.net (Richardson) on a public service | Moot from Azure (cor.net blocks data-center addresses either way). The hybrid fetch keeps the browser User-Agent and HTTP/2 from the home machine. |
| Hybrid fetch depends on the owner's home machine | Fallbacks stay in place (Swagit for Richardson); the stale-source check flags an inbox that stops filling; the home fetcher's credential can only write to `inbox`. |
| Irving's Cloudflare check escalates (for example to something only a person can complete) | Report Irving as unavailable rather than work around it; keep watching for PrimeGov to go live. |
| Agenda platforms change without notice | Stale-source check (Phase 4); the per-platform readers are kept small and tested. |
| Database growth from large packets (for example, Dallas County's 1,750-page agendas) | Page-text retention limits (Phase 2). |
| Noise from broad terms across 42+ sources | Filters (Phase 1); tags, action tags and noise rules (Phase 5). |
| Custom domain | Live at https://agendas.urbancivic.cloud through Cloudflare (Phase 3). |

## Source notes

The README holds the per-source platform tables and the reasons blocked sources are excluded.
`config.yaml` comments record source-specific quirks such as archive IDs, file-name date formats,
and documents that are skipped.
