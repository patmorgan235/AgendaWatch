"""Adapters that list a city's meeting agendas for a date window.

Each source returns Document objects; the runner downloads and searches them.

  legistar   Granicus Legistar (Dallas, Carrollton, Farmers Branch, University Park).
             Uses the public Legistar Web API; falls back to the website calendar.
  novus      NovusAGENDA (Plano).
  link_page  Any page that links to agendas (Addison, Richardson's Swagit archive).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urljoin

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
                row = a.find_parent(["tr", "li", "article"]) or a.parent
                context = row.get_text(" ", strip=True) if row else text
                found.append((href, f"{context} || {text} || {href}"))
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
        for href, ctx in list(candidates.items())[: self.max_links]:
            row_text = ctx.split(" || ")[0]
            mdate = parse_date(row_text) or parse_date(href)
            body = re.sub(r"\b(Video|Agenda|Minutes|Packet|Download)\b", "", row_text)
            body = re.sub(r"[A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}h \d{2}m|\d{1,2}m \d{2}s", "", body)
            body = re.sub(r"\s+", " ", body).strip(" -|·") or "Meeting"
            body = body[:80]
            if _in_window(mdate, start, end) and _body_allowed(body, self.bodies):
                docs.append(Document(self.name, self.city, body, mdate, f"{body} agenda", href, "agenda"))
        return docs


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
        common = dict(name=name, city=city, http=http, bodies=bodies)
        if kind == "legistar":
            sources.append(LegistarSource(**common, **spec))
        elif kind == "novus":
            sources.append(NovusSource(**common, **spec))
        elif kind == "link_page":
            sources.append(LinkPageSource(**common, **spec))
        else:
            raise ValueError(f"Source '{name}' has unknown type '{kind}'")
    return sources
