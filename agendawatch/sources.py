"""Adapters that list a city's meeting agendas for a date window.

Each source returns Document objects; the runner downloads and searches them.

  legistar   Granicus Legistar (Dallas, Plano, Carrollton, Farmers Branch, University Park).
             Uses the public Legistar Web API; falls back to the website calendar.
  novus      NovusAGENDA (no city uses it now; Plano left it in July 2026).
  link_page  Any page that links to agendas (Addison, Richardson's Swagit archive).
  civicclerk CivicClerk (Rowlett, Garland). Uses the portal's public OData API.
  docspublished  Docs Published / DocAssembler (Fort Worth). Reads the public API's meeting
             list and each agenda's sections, items and attachment titles.
  granicus   Granicus ViewPublisher pages (Denton County): upcoming and archived meetings,
             agenda via AgendaViewer.php.
  civicweb   iCompass/Diligent CivicWeb (Dallas County). Walks the Document Center's
             Agendas folder: meeting type → year → "<Meeting> - <Mon DD YYYY> - Agenda - Html|Pdf".
  doc_table  A page with one table row per meeting and a column per document
             (Richardson's "Council Meeting Documents" page on cor.net).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .dates import parse_date
from .http import FetchError, Http


class SourceError(Exception):
    pass


@dataclass
class Document:
    source: str
    city: str
    body: str
    meeting_date: date | None
    title: str
    url: str
    kind: str = "agenda"
    detail_url: str | None = None
    inline_text: str | None = None  # searched directly, no download

    @property
    def is_upcoming(self) -> bool:
        return self.meeting_date is None or self.meeting_date >= date.today()


def _in_window(d: date | None, start: date, end: date) -> bool:
    return d is None or start <= d <= end


def _body_allowed(body: str, wanted: list[str]) -> bool:
    return not wanted or any(w.lower() in body.lower() for w in wanted)


@dataclass
class Source:
    name: str
    city: str
    http: Http
    bodies: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    platform: str = "generic"
    section: str = ""                # report section, e.g. "DCTA member cities"
    note: str = ""                   # shown in the report's Sources table (e.g. "fallback: posts late")

    def list_documents(self, start: date, end: date) -> list[Document]:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------- Legistar
@dataclass
class LegistarSource(Source):
    client: str = ""
    attachments: str = "none"        # none | names | download
    max_attachments: int = 25
    platform: str = "Legistar"
    api_base: str = "https://webapi.legistar.com/v1"

    def list_documents(self, start: date, end: date) -> list[Document]:
        try:
            return self._from_api(start, end)
        except (FetchError, SourceError) as exc:
            self.notes.append(f"Legistar API unavailable ({exc}); read the website calendar instead")
            return self._from_calendar(start, end)

    def _from_api(self, start: date, end: date) -> list[Document]:
        url = f"{self.api_base}/{self.client}/events"
        flt = (
            f"EventDate ge datetime'{start:%Y-%m-%d}' and "
            f"EventDate lt datetime'{end + timedelta(days=1):%Y-%m-%d}'"
        )
        events = self.http.get_json(url, params={"$filter": flt, "$orderby": "EventDate asc"})
        if not isinstance(events, list):
            raise SourceError(str(events)[:200])
        docs: list[Document] = []
        for ev in events:
            body = (ev.get("EventBodyName") or "Meeting").strip()
            if not _body_allowed(body, self.bodies):
                continue
            mdate = parse_date(ev.get("EventDate"))
            detail = ev.get("EventInSiteURL")
            agenda = ev.get("EventAgendaFile")
            if agenda:
                docs.append(Document(self.name, self.city, body, mdate, f"{body} agenda", agenda, "agenda", detail))
            if self.attachments in ("names", "download") and ev.get("EventId"):
                docs.extend(self._items(ev["EventId"], body, mdate, detail))
        return docs

    def _items(self, event_id: int, body: str, mdate: date | None, detail: str | None) -> list[Document]:
        url = f"{self.api_base}/{self.client}/events/{event_id}/eventitems"
        try:
            items = self.http.get_json(url, params={"AgendaNote": 1, "Attachments": 1})
        except FetchError as exc:
            self.notes.append(f"Couldn't read agenda items for event {event_id}: {exc}")
            return []
        docs: list[Document] = []
        lines: list[str] = []
        for it in items or []:
            title = (it.get("EventItemTitle") or "").strip()
            num = (it.get("EventItemAgendaNumber") or "").strip()
            atts = it.get("EventItemMatterAttachments") or []
            if title:
                lines.append(f"{num} {title}".strip())
            for att in atts:
                name = (att.get("MatterAttachmentName") or "").strip()
                link = att.get("MatterAttachmentHyperlink")
                if name:
                    lines.append(f"    Attachment: {name}")
                if self.attachments == "download" and link and len(docs) < self.max_attachments:
                    docs.append(Document(self.name, self.city, body, mdate, f"{body}: {name or 'attachment'}", link, "attachment", detail))
        if lines:
            docs.insert(0, Document(
                self.name, self.city, body, mdate, f"{body} agenda items & attachment titles",
                f"{detail or self.client}#items-{event_id}", "items", detail, inline_text="\n".join(lines),
            ))
        return docs

    def _from_calendar(self, start: date, end: date) -> list[Document]:
        base = f"https://{self.client}.legistar.com/"
        try:
            html = self.http.get_text(base + "Calendar.aspx")
        except FetchError as exc:
            raise SourceError(f"Legistar website unreachable too: {exc}") from exc
        soup = BeautifulSoup(html, "html.parser")
        docs, seen = [], set()
        for a in soup.find_all("a", href=re.compile(r"View\.ashx\?M=A&", re.I)):
            href = urljoin(base, a["href"])
            if href in seen:
                continue
            seen.add(href)
            row = a.find_parent("tr")
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")] if row else []
            mdate = parse_date(" ".join(cells))
            body = next((c for c in cells if c and not parse_date(c)), "Meeting")
            detail_a = row.find("a", href=re.compile("MeetingDetail", re.I)) if row else None
            detail = urljoin(base, detail_a["href"]) if detail_a else None
            if _in_window(mdate, start, end) and _body_allowed(body, self.bodies):
                docs.append(Document(self.name, self.city, body, mdate, f"{body} agenda", href, "agenda", detail))
        if not docs and not seen:
            raise SourceError("No agenda links found on the Legistar calendar page")
        return docs


# ---------------------------------------------------------------- NovusAGENDA
@dataclass
class NovusSource(Source):
    base_url: str = ""
    platform: str = "NovusAGENDA"

    _LABELS = {"online agenda", "agenda pdf", "meeting location", "view agenda html", "download agenda"}

    def list_documents(self, start: date, end: date) -> list[Document]:
        base = self.base_url.rstrip("/") + "/"
        html, last_exc = None, None
        for page in ("MeetingsResponsive.aspx", "meetingsresponsive.aspx", ""):
            try:
                html = self.http.get_text(base + page)
                if "DisplayAgendaPDF" in html:
                    break
            except FetchError as exc:
                last_exc = exc
        if not html:
            raise SourceError(f"NovusAGENDA site unreachable: {last_exc}")
        soup = BeautifulSoup(html, "html.parser")
        docs, seen = [], set()
        for a in soup.find_all("a", href=re.compile(r"DisplayAgendaPDF\.ashx\?MeetingID=\d+", re.I)):
            href = urljoin(base, a["href"])
            if href in seen:
                continue
            seen.add(href)
            row = a.find_parent("tr")
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")] if row else []
            row_text = " ".join(cells)
            if "CANCELLED" in row_text.upper():
                continue
            mdate = parse_date(row_text)
            body = next(
                (c.split("  ")[0] for c in cells if c and not parse_date(c) and c.lower() not in self._LABELS),
                "Meeting",
            )
            body = re.sub(r"\s+", " ", body)
            half = len(body) // 2
            if half and body[:half].strip() == body[half:].strip():  # label repeated on two lines
                body = body[:half].strip()
            if _in_window(mdate, start, end) and _body_allowed(body, self.bodies):
                docs.append(Document(self.name, self.city, body, mdate, f"{body} agenda", href, "agenda"))
        if not seen:
            raise SourceError("No agenda PDF links found on the NovusAGENDA page")
        return docs


# ---------------------------------------------------------------- generic link page
@dataclass
class LinkPageSource(Source):
    urls: list[str] = field(default_factory=list)
    link_pattern: str = r"\.pdf"
    link_text_pattern: str | None = None
    follow_pattern: str | None = None
    meeting_name: str | None = None   # use this instead of guessing the meeting name from the page
    skip_undated: bool = False        # ignore links with no date (e.g. old archive files)
    exclude_pattern: str | None = None  # skip links whose table row / list item matches this
    name_cell: int | None = None      # take the meeting name from this table cell (0 = first column)
    months_back: int = 1
    max_links: int = 60
    platform: str = "web page"

    def _expand(self) -> list[str]:
        out = []
        today = date.today()
        for u in self.urls:
            if "{month}" in u or "{year}" in u:
                # next month (agendas post ahead), this month, and months_back earlier
                y, m = today.year, today.month
                months = [(y + 1, 1) if m == 12 else (y, m + 1)]
                for i in range(self.months_back + 1):
                    mm, yy = m - i, y
                    while mm < 1:
                        mm += 12
                        yy -= 1
                    months.append((yy, mm))
                out.extend(u.format(year=yy, month=mm) for yy, mm in months)
            else:
                out.append(u)
        return list(dict.fromkeys(out))

    def _scan(self, page_url: str, html: str, href_re, text_re) -> tuple[list[tuple[str, str]], list[str]]:
        soup = BeautifulSoup(html, "html.parser")
        found, follow = [], []
        follow_re = re.compile(self.follow_pattern, re.I) if self.follow_pattern else None
        for a in soup.find_all("a", href=True):
            href = urljoin(page_url, a["href"])
            text = a.get_text(" ", strip=True)
            if href_re.search(href) and (not text_re or text_re.search(text)):
                # Prefer the table row: a link in a row's dropdown list still belongs to that meeting.
                row = a.find_parent("tr") or a.find_parent(["li", "article"]) or a.parent
                context = row.get_text(" ", strip=True) if row else text
                cell = ""
                if self.name_cell is not None and row is not None and row.name == "tr":
                    cells = row.find_all(["td", "th"], recursive=False)
                    if len(cells) > self.name_cell:
                        cell = cells[self.name_cell].get_text(" ", strip=True)
                found.append((href, f"{context} || {text} || {href} || {cell}"))
            elif follow_re and follow_re.search(href):
                follow.append(href)
        return found, follow

    def list_documents(self, start: date, end: date) -> list[Document]:
        href_re = re.compile(self.link_pattern, re.I)
        text_re = re.compile(self.link_text_pattern, re.I) if self.link_text_pattern else None
        candidates: dict[str, str] = {}
        errors, follow_queue = [], []
        for url in self._expand():
            try:
                html = self.http.get_text(url)
            except FetchError as exc:
                errors.append(str(exc))
                continue
            found, follow = self._scan(url, html, href_re, text_re)
            for href, ctx in found:
                candidates.setdefault(href, ctx)
            follow_queue.extend(follow)
        for url in list(dict.fromkeys(follow_queue))[: self.max_links]:
            try:
                html = self.http.get_text(url)
            except FetchError as exc:
                errors.append(str(exc))
                continue
            found, _ = self._scan(url, html, href_re, text_re)
            for href, ctx in found:
                candidates.setdefault(href, ctx)
        if not candidates and errors:
            raise SourceError("; ".join(dict.fromkeys(errors))[:300])
        if errors:
            self.notes.append(f"{len(errors)} page(s) couldn't be read")
        docs = []
        # Filter by date before capping, so a long archive on one page can't crowd out another page.
        for href, ctx in candidates.items():
            if len(docs) >= self.max_links:
                self.notes.append(f"Stopped at {self.max_links} agendas (max_links)")
                break
            row_text = ctx.split(" || ")[0]
            if self.exclude_pattern and re.search(self.exclude_pattern, ctx, re.I):
                continue
            mdate = parse_date(row_text) or parse_date(unquote(href))
            if mdate is None and self.skip_undated:
                continue
            cell = ctx.split(" || ")[3] if ctx.count(" || ") >= 3 else ""
            if self.meeting_name:
                body = self.meeting_name
            elif cell:
                body = re.sub(r"\s+", " ", cell).replace("&amp;", "&")[:80]
            else:
                body = re.sub(r"[—–-]\s*(?:Posted|Amended|Updated)\s+(?:[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}\s+)?\d{1,2}:\d{2}\s*[AP]M",
                              "", row_text, flags=re.I)
                body = re.sub(r"\b(Video|Agenda|Minutes|Packet|Download|Previous Versions|Stream|HTML|PDF)\b|▼", "", body, flags=re.I)
                body = re.sub(r"\(\s*\)", "", body)
                body = re.sub(r"\b\d{1,2}-\d{1,2}-\d{4}\b", "", body)
                body = re.sub(r"[A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}h \d{2}m|\d{1,2}m \d{2}s", "", body)
                body = re.sub(r"\s+", " ", body).strip(" -|·,") or "Meeting"
                body = body[:80]
            if _in_window(mdate, start, end) and _body_allowed(body, self.bodies):
                docs.append(Document(self.name, self.city, body, mdate, f"{body} agenda", href, "agenda"))
        return docs


# ---------------------------------------------------------------- CivicClerk
@dataclass
class CivicClerkSource(Source):
    """CivicClerk portals (https://<tenant>.portal.civicclerk.com).

    The portal is a JavaScript app backed by a public OData API at
    https://<tenant>.api.civicclerk.com/v1. Each event lists its published files
    with a type such as "Agenda", "Agenda Packet" or "Minutes"; `files` picks which to read.
    """

    tenant: str = ""
    files: list[str] = field(default_factory=lambda: ["Agenda", "Agenda Packet"])
    max_pages: int = 10
    platform: str = "CivicClerk"

    def list_documents(self, start: date, end: date) -> list[Document]:
        api = f"https://{self.tenant}.api.civicclerk.com/v1"
        portal = f"https://{self.tenant}.portal.civicclerk.com"
        wanted = {f.lower() for f in self.files}
        url: str | None = f"{api}/Events"
        params: dict | None = {
            "$filter": (
                f"startDateTime ge {start:%Y-%m-%d}T00:00:00Z and "
                f"startDateTime lt {end + timedelta(days=1):%Y-%m-%d}T00:00:00Z"
            ),
            "$orderby": "startDateTime asc",
        }
        docs: list[Document] = []
        for _ in range(self.max_pages):
            data = self.http.get_json(url, params=params)
            if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                raise SourceError(f"Unexpected CivicClerk response: {str(data)[:200]}")
            for ev in data["value"]:
                name = (ev.get("eventName") or ev.get("agendaName") or "Meeting").strip()
                category = (ev.get("categoryName") or ev.get("eventCategoryName") or "").strip()
                if not _body_allowed(f"{category} {name}", self.bodies):
                    continue
                if re.search(r"\bcancel+ed\b", name, re.I):
                    continue
                # Times are local wall-clock times labelled Z, so the date part is the meeting date.
                mdate = parse_date(ev.get("startDateTime") or ev.get("eventDate"))
                detail = f"{portal}/event/{ev.get('id')}/files" if ev.get("id") is not None else portal
                for f in ev.get("publishedFiles") or []:
                    ftype = (f.get("type") or "").strip()
                    if ftype.lower() not in wanted or f.get("fileId") is None:
                        continue
                    href = f"{api}/Meetings/GetMeetingFileStream(fileId={f['fileId']},plainText=false)"
                    kind = "packet" if "packet" in ftype.lower() else ftype.lower()
                    title = name if ftype.lower() in name.lower() else f"{name} {ftype.lower()}"
                    docs.append(Document(self.name, self.city, name, mdate, title, href, kind, detail))
            url, params = data.get("@odata.nextLink"), None
            if not url:
                break
        else:
            self.notes.append(f"Stopped after {self.max_pages} pages of events")
        return docs


# ---------------------------------------------------------------- Docs Published
def _to_local(utc_iso: str | None, tz_name: str) -> date | None:
    """Docs Published gives meeting times in UTC ("2026-11-11T00:00:00" is Nov 10, 6 pm Central)."""
    if not utc_iso:
        return None
    try:
        dt = datetime.fromisoformat(utc_iso[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return parse_date(utc_iso)
    try:
        from zoneinfo import ZoneInfo

        return dt.astimezone(ZoneInfo(tz_name)).date()
    except Exception:  # noqa: BLE001 - no tz database (some Windows installs): assume US Central
        return (dt - timedelta(hours=6)).date()


@dataclass
class DocsPublishedSource(Source):
    """https://docspublished.io/<org> portals, backed by https://api.docassembler.io/api.

    The meeting list and each agenda's outline (sections, item titles, attachment titles)
    come from the public API. Each agenda becomes one searchable text document; the
    report links to the agenda's page on docspublished.io.
    """

    org: str = ""
    api_base: str = "https://api.docassembler.io/api"
    site_base: str = "https://docspublished.io"
    document_types: list[str] = field(default_factory=lambda: ["Agenda"])
    timezone: str = "America/Chicago"
    attempts: int = 3                # the API drops connections now and then
    platform: str = "Docs Published"

    def _get(self, path: str):
        last: Exception | None = None
        for i in range(self.attempts):
            try:
                return self.http.get_json(f"{self.api_base}/{path}")
            except FetchError as exc:
                last = exc
                if "couldn't connect" not in str(exc) and "timed out" not in str(exc):
                    raise
                time.sleep(2 * (i + 1))
        raise last  # type: ignore[misc]

    @staticmethod
    def _outline(doc: dict) -> str:
        lines = [doc.get("Title") or "", doc.get("MeetingType") or "",
                 " ".join(filter(None, [doc.get("MeetingVenue"), doc.get("MeetingRoom"), doc.get("MeetingLocation")]))]
        for sec in doc.get("WP_Sections") or []:
            lines.append(f"{sec.get('Number') or ''} {sec.get('Title') or ''}".strip())
            for it in sec.get("WP_Items") or []:
                lines.append(f"  {it.get('Number') or ''} {it.get('Title') or ''}".rstrip())
                for att in it.get("WP_Attachments") or []:
                    if att.get("Title"):
                        lines.append(f"      Attachment: {att['Title']}")
        return "\n".join(line for line in lines if line.strip())

    def list_documents(self, start: date, end: date) -> list[Document]:
        org = self._get(f"organisation/{self.org}")
        org_id = org.get("Id") if isinstance(org, dict) else None
        if not org_id:
            raise SourceError(f"Docs Published organisation '{self.org}' not found")
        listing = self._get(f"documents/{org_id}")
        if not isinstance(listing, list):
            raise SourceError(f"Unexpected Docs Published response: {str(listing)[:200]}")
        wanted = {t.lower() for t in self.document_types}
        docs: list[Document] = []
        for ev in listing:
            dtype = ((ev.get("WP_DocumentType") or {}).get("Name") or "").lower()
            doc_id = ev.get("AgendaDocumentId")
            body = (ev.get("MeetingType") or "Meeting").strip()
            mdate = _to_local(ev.get("MeetingDate"), self.timezone)
            if dtype not in wanted or not doc_id or ev.get("IsArchived"):
                continue
            if not (_in_window(mdate, start, end) and _body_allowed(body, self.bodies)):
                continue
            try:
                outline = self._outline(self._get(f"document/{doc_id}"))
            except FetchError as exc:
                self.notes.append(f"Couldn't read {ev.get('DocumentTitle') or body}: {exc}")
                continue
            page = f"{self.site_base}/{self.org}/document/{doc_id}"
            docs.append(Document(self.name, self.city, body, mdate, f"{body} agenda", page, "items", page,
                                 inline_text=outline))
        return docs


# ---------------------------------------------------------------- Granicus ViewPublisher
@dataclass
class GranicusSource(Source):
    """https://<name>.granicus.com/ViewPublisher.php?view_id=N

    Lists "Upcoming Events" (agenda link: AgendaViewer.php?...&event_id=) and archived
    meetings (agenda in a Documents dropdown: AgendaViewer.php?...&clip_id=). AgendaViewer
    returns the agenda as HTML or PDF.
    """

    url: str = ""
    platform: str = "Granicus"

    _AGENDA = re.compile(r"AgendaViewer\.php\?[^\"'\s>]*", re.I)

    def list_documents(self, start: date, end: date) -> list[Document]:
        html = self.http.get_text(self.url)
        soup = BeautifulSoup(html, "html.parser")
        docs: dict[str, Document] = {}
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            targets = [a.get("href", "") for a in row.find_all("a")] + [o.get("value", "") for o in row.find_all("option")]
            agenda = next((t for t in targets if self._AGENDA.search(t)), None)
            if not agenda:
                continue
            body = re.sub(r"\s+", " ", cells[0].get_text(" ", strip=True)) or "Meeting"
            mdate = parse_date(cells[1].get_text(" ", strip=True)) or parse_date(row.get_text(" ", strip=True))
            if mdate is None or not _in_window(mdate, start, end) or not _body_allowed(body, self.bodies):
                continue
            href = urljoin(self.url, agenda)
            docs.setdefault(href, Document(self.name, self.city, body, mdate, f"{body} agenda", href, "agenda", self.url))
        if not docs and "listingTable" not in html:
            raise SourceError("No Granicus meeting table found on the page")
        return list(docs.values())


# ---------------------------------------------------------------- CivicWeb
@dataclass
class CivicWebSource(Source):
    """CivicWeb (https://<name>.civicweb.net) agendas from the public Document Center.

    The Document Center has an "Agendas" folder with a subfolder per meeting type
    ("Commissioners Court", ...), a subfolder per year, and the agendas themselves named
    "<Meeting> - Nov 17 2026 - Agenda - Html" (and "- Pdf"). The Html version lists every
    agenda item and is small; the Pdf version can include all backup and run past 100 MB.
    """

    base_url: str = ""
    meeting_types: list[str] = field(default_factory=lambda: ["Commissioners Court"])
    agendas_folder: str = "Agendas"
    format: str = "html"            # html | pdf | both
    platform: str = "CivicWeb"

    _LINK = re.compile(r'<a[^>]+href="(/filepro/documents/\d+|/document/\d+)[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
    _NAME = re.compile(r"^(?P<body>.+?)\s*-\s*(?P<date>[^-]*?\d{4})\s*-\s*Agenda\s*-\s*(?P<fmt>Html|Pdf)\s*$", re.I)

    def _links(self, path: str) -> list[tuple[str, str]]:
        html = self.http.get_text(urljoin(self.base_url, path))
        out = []
        for m in self._LINK.finditer(html):
            text = re.sub(r"<[^>]+>|\s+", " ", m.group(2)).replace("&amp;", "&").strip()
            if m.group(1) != path and text:
                out.append((m.group(1), text))
        return out

    def _folder(self, links: list[tuple[str, str]], name: str) -> str | None:
        return next((h for h, t in links if h.startswith("/filepro/") and t.lower() == name.lower()), None)

    def list_documents(self, start: date, end: date) -> list[Document]:
        formats = {"html": {"html"}, "pdf": {"pdf"}}.get(self.format.lower(), {"html", "pdf"})
        agendas = self._folder(self._links("/filepro/documents"), self.agendas_folder)
        if not agendas:
            raise SourceError(f"No '{self.agendas_folder}' folder in the CivicWeb Document Center")
        type_links = self._links(agendas)
        years = {str(y) for y in range(start.year, end.year + 1)}
        docs: list[Document] = []
        for mtype in self.meeting_types:
            folder = self._folder(type_links, mtype)
            if not folder:
                self.notes.append(f"No agenda folder named '{mtype}'")
                continue
            for year_path, year in self._links(folder):
                if year not in years or not year_path.startswith("/filepro/"):
                    continue
                for doc_path, name in self._links(year_path):
                    m = self._NAME.match(name)
                    if not doc_path.startswith("/document/") or not m or m["fmt"].lower() not in formats:
                        continue
                    mdate = parse_date(m["date"])
                    body = m["body"].strip()
                    if _in_window(mdate, start, end) and _body_allowed(body, self.bodies):
                        title = f"{body} agenda" + (" (PDF)" if m["fmt"].lower() == "pdf" and len(formats) > 1 else "")
                        docs.append(Document(self.name, self.city, body, mdate, title,
                                             urljoin(self.base_url, doc_path), "agenda",
                                             urljoin(self.base_url, "/Portal/MeetingTypeList.aspx")))
        return docs


# ---------------------------------------------------------------- document table
@dataclass
class DocTableSource(Source):
    """A table of meetings: a date column, then columns of document links.

    `columns` names the header cells (case-insensitive, partial match) whose links to read,
    e.g. ["agenda", "packet"]. The meeting name comes from the first of those columns' link
    text ("City Council Meeting", "Special Called Meeting: ..."). Other columns (presentations,
    video, minutes) are ignored.
    """

    urls: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=lambda: ["agenda", "packet"])
    link_pattern: str = r"."
    skip_pattern: str = r"\b(cancel+ed|no meeting)\b"
    platform: str = "document table"

    def _parse(self, page_url: str, html: str, start: date, end: date, href_re, skip_re) -> list[Document]:
        soup = BeautifulSoup(html, "html.parser")
        wanted = [c.lower() for c in self.columns]
        docs: list[Document] = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
            cols = [(w, next((i for i, h in enumerate(headers) if w in h), None)) for w in wanted]
            cols = [(w, i) for w, i in cols if i is not None]
            if not cols:
                continue
            for row in rows[1:]:
                cells = row.find_all(["td", "th"])
                if not cells:
                    continue
                row_text = row.get_text(" ", strip=True)
                mdate = parse_date(cells[0].get_text(" ", strip=True)) or parse_date(row_text)
                if skip_re.search(row_text):
                    continue
                links = []
                for w, i in cols:
                    if i < len(cells):
                        for a in cells[i].find_all("a", href=True):
                            href = urljoin(page_url, a["href"])
                            if href_re.search(href):
                                links.append((w, href, re.sub(r"\s+", " ", a.get_text(" ", strip=True))))
                if not links:
                    continue
                body = (links[0][2] or "Meeting")[:80]
                if not (_in_window(mdate, start, end) and _body_allowed(body, self.bodies)):
                    continue
                for w, href, text in links:
                    title = f"{body} {w}" if w.lower() not in body.lower() else body
                    docs.append(Document(self.name, self.city, body, mdate, title, href, w, page_url))
        return docs

    def list_documents(self, start: date, end: date) -> list[Document]:
        href_re = re.compile(self.link_pattern, re.I)
        skip_re = re.compile(self.skip_pattern, re.I)
        docs, errors, tables_seen = [], [], False
        for url in self.urls:
            try:
                html = self.http.get_text(url)
            except FetchError as exc:
                errors.append(str(exc))
                continue
            tables_seen = tables_seen or "<table" in html.lower()
            docs.extend(self._parse(url, html, start, end, href_re, skip_re))
        if errors and not docs:
            raise SourceError("; ".join(dict.fromkeys(errors))[:300])
        if errors:
            self.notes.append(f"{len(errors)} page(s) couldn't be read")
        if not tables_seen and not errors:
            raise SourceError("No meeting table found on the page (has the site's layout changed?)")
        return list({d.url: d for d in docs}.values())


# ---------------------------------------------------------------- factory
def build_sources(specs: list[dict], http: Http) -> list[Source]:
    sources: list[Source] = []
    for spec in specs:
        spec = dict(spec)
        if spec.pop("enabled", True) is False:
            continue
        kind = spec.pop("type")
        name = spec.pop("name")
        city = spec.pop("city", name)
        bodies = spec.pop("bodies", []) or []
        section = str(spec.pop("section", "") or "")
        note = str(spec.pop("note", "") or "")
        user_agent = spec.pop("user_agent", None)
        if user_agent:
            hosts = {urlparse(u).netloc for u in spec.get("urls", []) + [spec.get("base_url") or ""]}
            for host in filter(None, hosts):
                if user_agent == "browser":
                    http.set_browser_host(host)
                else:
                    http.set_host_headers(host, {"User-Agent": str(user_agent)})
        common = dict(name=name, city=city, http=http, bodies=bodies, section=section, note=note)
        if kind == "legistar":
            sources.append(LegistarSource(**common, **spec))
        elif kind == "novus":
            sources.append(NovusSource(**common, **spec))
        elif kind == "link_page":
            sources.append(LinkPageSource(**common, **spec))
        elif kind == "civicclerk":
            sources.append(CivicClerkSource(**common, **spec))
        elif kind == "docspublished":
            sources.append(DocsPublishedSource(**common, **spec))
        elif kind == "granicus":
            sources.append(GranicusSource(**common, **spec))
        elif kind == "civicweb":
            sources.append(CivicWebSource(**common, **spec))
        elif kind == "doc_table":
            sources.append(DocTableSource(**common, **spec))
        else:
            raise ValueError(f"Source '{name}' has unknown type '{kind}'")
    return sources
