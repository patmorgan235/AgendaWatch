# agenda-watch

Checks North Texas agenda sites every day, searches each agenda for your key terms, and writes a report of new mentions, grouped into sections.

**DART member cities**

| City | Platform | How it's read |
|---|---|---|
| Dallas | Legistar (`cityofdallas`) | Legistar Web API, website calendar as fallback |
| Farmers Branch | Legistar (`farmersbranch`) | same |
| Carrollton | Legistar (`carrolltontx`) | same |
| University Park | Legistar (`uptexas`) | same |
| Plano | Legistar (`plano`) | same (moved from NovusAGENDA in July 2026) |
| Richardson | cor.net Council Meeting Documents, plus Swagit video archive | cor.net: agenda + packet PDFs ~6 days ahead (blocked from cloud hosting). Swagit: fallback that also covers boards and commissions, but lists meetings around meeting day |
| Addison | Addison agenda portal | meeting pages → agenda packet PDFs |
| Rowlett | CivicClerk (`rowletttx`) | CivicClerk API → agenda + packet PDFs |
| Garland | CivicClerk (`garlandtx`) | same |

**DCTA member cities**

| City | Platform | How it's read |
|---|---|---|
| Denton | Legistar (`denton-tx`) | Legistar Web API |
| Lewisville | Legistar (`cityoflewisville`) | same |
| Highland Village | CivicPlus Archive Center | City Council and special meeting agenda PDFs |

**Trinity Metro member & associate cities**

Fort Worth is the full member; the others are partial/associate members through interlocal agreements.

| City | Platform | How it's read |
|---|---|---|
| Fort Worth | Docs Published (`fortworthtexas`) | public API → each agenda's sections, item titles and attachment titles (moved from Legistar in June 2026) |
| Grapevine | CivicPlus Archive Center | City Council agendas + packets |
| North Richland Hills | Legistar (`nrhtx`) | Legistar Web API |
| Forest Hill | CivicClerk (`foresthilltx`) | CivicClerk API → agenda PDFs |
| River Oaks | CivicPlus Agenda Center | agenda PDFs (municipal court dockets skipped) |

**Regional**

| Body | Platform | How it's read |
|---|---|---|
| Regional Transportation Council | NCTCOG RTC page | agenda + full meeting packet PDFs |

**Counties**

| County | Platform | How it's read |
|---|---|---|
| Dallas County | CivicWeb Commissioners Court | HTML agenda + full PDF with briefings, from the Document Center |
| Collin County | eAgenda portal | each meeting's agenda page (full item descriptions) |
| Denton County | Granicus ViewPublisher | Commissioners Court agendas (upcoming + archived) |

Tarrant County isn't included: its agendas are only in an AgendaLink portal whose data API requires a token embedded in the app.

**Eligible to join a transit agency**

Cities that could join DART (they border a DART member city), Trinity Metro (they border Fort Worth) or DCTA (they're in Denton County) by referendum, but haven't. Borders were checked against Census TIGERweb boundaries in November 2026.

| City | Eligible for | Platform |
|---|---|---|
| Coppell | DART | Legistar (`coppell`) |
| McKinney | DART | Legistar (`mckinney`) |
| Keller | Trinity Metro | Legistar (`cityofkeller`) |
| Mesquite | DART | CivicPlus Agenda Center |
| DeSoto | DART | CivicPlus Agenda Center |
| Haltom City | Trinity Metro | CivicPlus Agenda Center |
| Benbrook | Trinity Metro | CivicPlus Agenda Center |
| Cedar Hill | DART | destinyhosted eAgenda |
| Lancaster | DART | destinyhosted eAgenda |
| Little Elm | DCTA | destinyhosted eAgenda |
| Duncanville | DART | CivicClerk |
| Euless | DART, Trinity Metro | CivicClerk |
| Sachse | DART | CivicClerk |
| Crowley | Trinity Metro | CivicClerk |
| White Settlement | Trinity Metro | CivicClerk |
| Watauga | Trinity Metro | CivicClerk |
| Flower Mound | DCTA | CivicClerk |
| Celina | DCTA | CivicClerk |
| Murphy | DART | CivicWeb |
| Frisco | DART, DCTA | OnBase Agenda Online |
| Hurst | Trinity Metro | agenda table on the city site |

Not read yet: Grand Prairie, The Colony, Burleson and Prosper post through Municode Meetings, which refuses the tool (Cloudflare challenge / dropped connections); Allen and Saginaw use an AgendaLink portal whose API needs a token embedded in the app; Arlington's agenda list is loaded by script; Rockwall's and Corinth's sites didn't respond.

Terms: DART, Dallas Area Rapid Transit, Transit, GMP, General Mobility Program, DCTA, Denton County Transportation Authority, Trinity Metro, Paratransit, Streetcar.

## Setup (once)

Needs Python 3.10 or newer.

```bash
cd agenda-watch
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Then check that every city's source can be read from your network:

```bash
python agenda_watch.py check
```

Each city should list a few recent or upcoming agendas. If one shows an error, see **Troubleshooting**.

First real run, looking back 90 days to build up history:

```bash
python agenda_watch.py run --backfill 90 --open
```

## Daily use

```bash
python agenda_watch.py run --open
```

Each run writes a small website into `reports/`:

| File | What it is |
|---|---|
| `index.html` (also `latest.html`) | The report page. Its data is built in, so it works opened straight from disk or hosted. |
| `data.json` | Every match for meetings in the report window, grouped by meeting, with when each was first found. |
| `feed.xml` | Atom/RSS feed of new mentions — subscribe in any feed reader. |
| `upcoming.ics` | Calendar feed of upcoming meetings whose agendas mention your terms. |
| `agenda-report-YYYY-MM-DD.html` | That day's snapshot of the page. |

The page shows **every** mention for meetings in the window (10 days back, 60 ahead by default), not just the ones found by the latest run, grouped into sections (DART member cities, DCTA member cities, Trinity Metro member & associate cities, Regional, Counties, Eligible to join a transit agency). You can:

- show **upcoming meetings only** (on by default) or include recent past ones;
- show only what's **new since your last visit** (remembered by your browser; on a first visit, new in the latest run);
- filter by section, city or meeting body, and term, or search the snippets;
- share a filtered view (filters are kept in the page's address) or link to one meeting (`#m-…`).

Each meeting shows how many days away it is, which terms it mentions, the matching passages with a link to the right page of the PDF, and links to the agenda, packet and meeting page. **Sources** at the bottom lists what was checked and anything that couldn't be read. Each source's `section:` in `config.yaml` decides where it appears.

The same passage is never reported as new twice, even when a city re-posts an amended agenda.

If the site will be hosted, set `site_url` in `config.yaml` (for example `https://agendas.example.org`) so the feeds contain full links.

### Search everything downloaded so far

```bash
python agenda_watch.py search "Silver Line"
python agenda_watch.py search "bus (rapid|stop)" --regex --city Plano
```

## Run it automatically every morning

**macOS / Linux (cron)** — run `crontab -e` and add (adjust the path):

```
43 6 * * * cd /path/to/agenda-watch && .venv/bin/python agenda_watch.py --quiet run >> data/cron.log 2>&1
```

**Windows (Task Scheduler)** — in Command Prompt:

```
schtasks /create /tn "Agenda Watch" /sc daily /st 06:43 /tr "C:\path\to\agenda-watch\.venv\Scripts\python.exe C:\path\to\agenda-watch\agenda_watch.py --quiet run"
```

The computer must be on at that time. Open `reports/index.html` (bookmark it) whenever you want the latest report.

## Hosted runs (container)

`python agenda_watch.py job` is the hosted version of `run`: it takes a run lock, downloads the database from storage, runs, saves the database, publishes the site files, and releases the lock. The `Dockerfile` builds an image whose default command is `job`.

```bash
podman build -t agendawatch .                        # or docker; OCR for scanned PDFs is built in (--build-arg WITH_OCR=false to leave it out)
podman run --rm -v ./hosted:/data -e AGENDAWATCH_LOCAL_DIR=/data agendawatch
```

Storage is picked with `AGENDAWATCH_STORAGE`:

| Value | Settings | State (database) | Site |
|---|---|---|---|
| `local` (default) | `AGENDAWATCH_LOCAL_DIR` (default `hosted`) | `<dir>/state/` | `<dir>/site/` |
| `azure` | `AGENDAWATCH_AZURE_ACCOUNT_URL` (or `AZURE_STORAGE_CONNECTION_STRING`), `AGENDAWATCH_AZURE_CLIENT_ID` for a user-assigned managed identity, `AGENDAWATCH_AZURE_STATE_CONTAINER` (default `state`), `AGENDAWATCH_AZURE_SITE_CONTAINER` (default `$web`) | private container | static website container |
| `s3` | `AGENDAWATCH_S3_STATE_BUCKET`, `AGENDAWATCH_S3_SITE_BUCKET`, optional `AGENDAWATCH_S3_ENDPOINT_URL` (R2, B2, MinIO) and `_STATE_PREFIX` / `_SITE_PREFIX` | bucket | bucket |

Other environment variables:

- `AGENDAWATCH_SETTING_<NAME>` overrides any `settings:` value in `config.yaml`, parsed as YAML, e.g. `AGENDAWATCH_SETTING_SITE_URL=https://agendas.example.org`, `AGENDAWATCH_SETTING_PAGE_RETENTION_DAYS=90`.
- `AGENDAWATCH_CONFIG`: path to the config file (the image uses its built-in copy).
- `AGENDAWATCH_LOG_FORMAT=json`: one JSON object per log line (the image's default), including a summary line per source.
- `AGENDAWATCH_LOCK_TTL_HOURS` (default 4): a lock older than this is assumed to be left by a crashed run and taken over.

Exit codes: `0` success (even if some sources couldn't be read; they're listed in the logs and the report), `3` every source failed (likely a network problem), `75` another run still holds the lock, `1` unexpected error.

To keep the hosted database small, set `page_retention_days` (e.g. 90): stored page text for older meetings is dropped and the file compacted. Matches are always kept, and dropped agendas aren't downloaded again.

## Changing terms or cities

Everything is in `config.yaml`.

- **Add a term:** add `- name: Silver Line` under `terms`. Plain phrases match whole words, ignore case, and tolerate line breaks.
- **Acronyms:** use `case_sensitive: true` (as DART and GMP do) so "Dart Container" doesn't match.
- **False positives:** `exclude_if_nearby` skips a match when a phrase appears close by. GMP already skips "Guaranteed Maximum Price", which is common in construction contracts.
- **Fewer Dallas meetings:** Dallas posts agendas for dozens of boards. Uncomment the `bodies:` line to keep only the ones you care about.
- **Deeper Legistar search:** `attachments: names` also searches agenda item titles and attachment names; `download` also downloads and searches attachments (slower).
- **A page with one row per meeting:** use `type: doc_table` and list which columns to read in `columns` (see Richardson).
- **Another city:** if its agendas are at `<name>.portal.civicclerk.com`, copy the Rowlett block and change `tenant`. If it's on Legistar, copy a Legistar block and change `client` to the name before `.legistar.com`. For other sites, use `type: link_page` with the page that lists agendas and a `link_pattern` matching the agenda links.

## Scanned PDFs

Some packets are scanned images with no text. The report notes these under Sources. To read them, install `ocrmypdf` (macOS: `brew install ocrmypdf`; Ubuntu: `sudo apt install ocrmypdf`; Windows: see the ocrmypdf docs). The tool uses it automatically.

## Troubleshooting

- **A Legistar city shows "API unavailable"** — the tool falls back to the city's website calendar on its own, so this is usually just a note. If both fail, check that `https://<client>.legistar.com/Calendar.aspx` opens in your browser; if the city has moved platforms, update that source.
- **University Park or Farmers Branch errors** — my evidence that these two still use Legistar was older than the others. If `check` fails, find "Agendas" on the city site, see where it leads, and adjust the source (or set `enabled: false`).
- **Addison shows no agendas** — open `https://agendas.addisontx.gov/agenda_publish.cfm`, right-click an agenda packet link, copy its address, and make sure `link_pattern` matches it.
- **Richardson (cor.net) shows HTTP 403 or "needs HTTP/2"** — cor.net turns away anything that doesn't look like a browser, so this source sends a browser User-Agent over HTTP/2 (`user_agent: browser`). Make sure `pip install -r requirements.txt` installed `httpx[http2]`. If it still gets 403, the city has tightened its bot filter; set `enabled: true` on the "Richardson (video archive)" source to fall back to Swagit, which works but only lists meetings around when they're held. From a cloud server (e.g. Azure) cor.net and hursttx.gov return 403 regardless — they block data-center addresses — so Richardson falls back to its Swagit archive and Hurst is skipped there.

## How it works

```
agenda_watch.py          command line (run / check / search)
config.yaml              terms, cities, settings
agendawatch/sources.py   Legistar, CivicClerk, CivicWeb, NovusAGENDA, link-page and document-table adapters
agendawatch/extract.py   PDF/HTML → per-page text (OCR when needed)
agendawatch/matcher.py   term matching, false-positive filters, snippets
agendawatch/store.py     SQLite: agendas read, page text, matches already reported
agendawatch/site.py      data.json, report page, Atom and calendar feeds
agendawatch/site_template.html  the report page (filters run in the browser)
agendawatch/report.py    writes the site; console summary
agendawatch/job.py       hosted run: lock, fetch database, run, save, publish
agendawatch/storage.py   local, Azure Blob and S3-compatible storage + run lock
tests/                   pytest suite with fixtures modeled on each platform's pages
```

Run the tests with `pip install pytest reportlab && pytest`.
