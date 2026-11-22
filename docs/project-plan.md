# AgendaWatch — Project Plan

_Last updated: November 22, 2026_

## Goal

Tell North Texas transit advocates about transit-related items going before local government
bodies **before the meetings happen**, early enough to read the item, organize, and comment or
speak.

A searchable history of past agenda items is useful but secondary.

## Audience

- **Primary:** transit advocates.
- The site is **public**.
- The project owner maintains the term list and the source configuration.

## Where we are (November 2026)

- A Python tool reads 42 agenda sources daily, searches them for transit terms, and writes a
  local HTML report.
- Report sections: DART member cities, DCTA member cities, Trinity Metro member & associate
  cities, Regional (RTC), Counties, and Eligible to join a transit agency.
- Platforms supported: Legistar, CivicClerk, CivicWeb, CivicPlus Agenda Center and Archive
  Center, destinyhosted/eAgenda, Granicus ViewPublisher, Docs Published (Fort Worth), OnBase
  Agenda Online (Frisco), and generic link pages and document tables.
- Each passage is reported once, by meeting. Amended agendas only surface genuinely new text.
- Runs from a local machine; the report is a local file.

### Known gaps

| Gap | Notes |
|---|---|
| Transit agency boards not watched | DART, Trinity Metro and DCTA boards, plus NCTCOG's Surface Transportation Technical Committee (STTC), which vets items before the RTC. |
| Blocked sources | Irving, Tarrant County, Grand Prairie, The Colony, Burleson, Prosper, Allen and Saginaw sit behind Cloudflare challenges, Municode Meetings, or token-only AgendaLink APIs. |
| Unresolved sources | Arlington (its agenda list is loaded by script), Rockwall and Corinth (sites unreachable). |
| Titles only | Fort Worth, Dallas County's HTML agenda, and Dallas Legistar with attachments off give item titles but not staff reports. |
| ~~Report hides earlier finds~~ | Fixed in Phase 1: the page shows every match for meetings in the window. |
| No push delivery | Nobody is told when something new appears. |
| No lead-time measurement | We don't know how many days of notice each source actually gives. |
| Silent source drift | Plano and Fort Worth changed platforms in 2026 without notice; nothing flags a source that goes quiet. |

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Hosting model | **Option B:** a scheduled container job plus a static site | More control than GitHub Actions (memory for large PDFs, OCR tools in the image, predictable start times). |
| Cloud | **Azure** (owner's most familiar platform) | Storage is kept behind an interface so it can move to S3 or R2 later. |
| Report delivery | Static site built from a published JSON data file | Filtering happens in the browser; RSS and calendar feeds come from the same data; fixes the hidden-earlier-finds gap. |
| Term list | Maintained by the owner in `config.yaml` | No admin interface needed. |

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

### Phase 0 — Cloud reachability test

Government sites sometimes block data-center IP addresses. This could change the hosting plan,
so it comes first.

- [ ] Build a throwaway container and run `agenda_watch.py check` once from Azure (Container Instances).
- [ ] Compare the results source by source with a run from a home connection.
- [ ] Decide: all of it on Azure, or a self-hosted runner for the scraping with the site still on Azure.

**Done when:** a table records which sources work from Azure, and the hosting decision is confirmed or revised.

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

### Phase 2 — Packaging

- [ ] Dockerfile (Python 3.12, optional `ocrmypdf` and Tesseract).
- [ ] Storage interface with `local`, `azure-blob` and `s3` backends, selected by environment variable.
- [ ] Run lock: an Azure blob lease, or a lock object on S3.
- [ ] All settings overridable by environment variables (storage, schedule window, size limits).
- [ ] **Database growth:**
  - drop or compress page text for meetings older than N days (configurable);
  - keep the matches indefinitely.
- [ ] Structured logging: one summary line per source, with a non-zero exit code if every source fails.

**Done when:** `docker run` with local storage produces the same site as a local run, and the database stays under a set size budget.

### Phase 3 — Azure deployment

- [ ] **Bicep templates:**
  - a storage account with the static website enabled and a private `state` container;
  - a Container Apps environment and a scheduled job;
  - a user-assigned managed identity with only the storage roles it needs;
  - Log Analytics.
- [ ] GitHub Actions: build and push the image on push to `main`; deploy the infrastructure manually or on tag.
- [ ] Optional: a custom domain through Front Door or a CDN.
- [ ] A short runbook: how to trigger a run, read the logs, and roll back the image.

**Done when:** the daily job runs unattended, the site updates, and a deliberately broken run raises an alert.

### Phase 4 — Operations and reliability

- [ ] An alert when a run fails.
- [ ] **Stale-source check:** flag any source with no new agendas in 30 days (configurable) in the report and logs.
- [ ] **Lead time per source:** median days between first seeing an agenda and its meeting, shown in the Sources table.
- [ ] A source-health history (listed, read, failed, errors) kept across runs.

### Phase 5 — Coverage

- [ ] Add the transit agency boards: DART Board and committees, Trinity Metro Board, DCTA Board, and NCTCOG STTC.
- [ ] Revisit the blocked and unresolved sources (Arlington, Rockwall, Corinth), and contact cities about the blocked ones (Irving, Tarrant County, the Municode Meetings and AgendaLink cities).
- [ ] Read deeper where possible: Legistar attachments for key bodies, and Fort Worth staff reports if a sanctioned way to read them appears.

### Later

- Searchable history: a static search page or a published read-only copy of the database.
- Email or Slack alerts for new matches.
- Ranking or triage: separate action items from passing mentions; reduce noise from broad terms such as "Transit".

## Risks and open questions

| Risk or question | Mitigation or next step |
|---|---|
| Data-center IP addresses get blocked by city sites | Phase 0 test; fall back to a self-hosted runner for scraping. |
| Browser mode for cor.net (Richardson) on a public service | Decide before launch: keep it, or ask Richardson to allow the tool's User-Agent. |
| Agenda platforms change without notice | Stale-source check (Phase 4); the per-platform readers are kept small and tested. |
| Database growth from large packets (for example, Dallas County's 1,750-page agendas) | Page-text retention limits (Phase 2). |
| Noise from broad terms across 42+ sources | Filters (Phase 1); triage (Later). |
| Custom domain | To decide before Phase 3. |

## Source notes

The README holds the per-source platform tables and the reasons blocked sources are excluded.
`config.yaml` comments record source-specific quirks such as archive IDs, file-name date formats,
and documents that are skipped.
