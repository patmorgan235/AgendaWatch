# agenda-watch

Checks nine North Texas city agenda sites every day, searches each agenda for your key terms, and writes a report of new mentions.

| City | Platform | How it's read |
|---|---|---|
| Dallas | Legistar (`cityofdallas`) | Legistar Web API, website calendar as fallback |
| Farmers Branch | Legistar (`farmersbranch`) | same |
| Carrollton | Legistar (`carrolltontx`) | same |
| University Park | Legistar (`uptexas`) | same |
| Plano | Legistar (`plano`) | same (moved from NovusAGENDA in July 2026) |
| Richardson | cor.net Council Meeting Documents | agenda + packet PDFs, posted ~6 days ahead (Swagit video archive kept in config, disabled) |
| Addison | Addison agenda portal | meeting pages → agenda packet PDFs |
| Rowlett | CivicClerk (`rowletttx`) | CivicClerk API → agenda + packet PDFs |
| Garland | CivicClerk (`garlandtx`) | same |

Terms: DART, Dallas Area Rapid Transit, Transit, GMP, General Mobility Program, Streetcar.

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

The report is saved to `reports/agenda-report-YYYY-MM-DD.html`, and `reports/latest.html` always holds the newest one. It has three parts:

- **New mentions** — matches not seen in any earlier report, grouped by meeting, with the surrounding text and a link that opens the PDF at the right page.
- **Upcoming meetings that mention your terms** — every posted future agenda with a match, new or not.
- **Sources** — what was checked per city, and any site that couldn't be read.

The same passage is never reported twice, even when a city re-posts an amended agenda. Only genuinely new items show up as new.

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

The computer must be on at that time. Open `reports/latest.html` (bookmark it) whenever you want the day's report.

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
- **Richardson (cor.net) shows HTTP 403 or "needs HTTP/2"** — cor.net turns away anything that doesn't look like a browser, so this source sends a browser User-Agent over HTTP/2 (`user_agent: browser`). Make sure `pip install -r requirements.txt` installed `httpx[http2]`. If it still gets 403, the city has tightened its bot filter; set `enabled: true` on the "Richardson (video archive)" source to fall back to Swagit, which works but only lists meetings around when they're held.

## How it works

```
agenda_watch.py          command line (run / check / search)
config.yaml              terms, cities, settings
agendawatch/sources.py   Legistar, CivicClerk, NovusAGENDA, link-page and document-table adapters
agendawatch/extract.py   PDF/HTML → per-page text (OCR when needed)
agendawatch/matcher.py   term matching, false-positive filters, snippets
agendawatch/store.py     SQLite: agendas read, page text, matches already reported
agendawatch/report.py    HTML report + console summary
tests/                   pytest suite with fixtures modeled on each platform's pages
```

Run the tests with `pip install pytest reportlab && pytest`.
