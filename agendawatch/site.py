"""Public site output: data.json, a single-page report, an Atom feed and a calendar feed.

Everything is built from one data structure (build_data), so the page, the feeds and any future
consumer agree. The page embeds its data, so it works opened from disk as well as when hosted.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from .runner import RunResult

OTHER = "Other sources"
FEED_TAG = "tag:agendawatch,2026"


# ---------------------------------------------------------------- data
def _meeting_id(source: str, body: str, meeting_date: str | None) -> str:
    return hashlib.sha1(f"{source}|{body}|{meeting_date}".encode()).hexdigest()[:12]


def _page_link(url: str, page: int | None, kind: str) -> str:
    if kind == "items" or "#" in url or not page:
        return url
    return f"{url}#page={page}"


def section_order(res: RunResult) -> list[str]:
    return list(dict.fromkeys([s.section or OTHER for s in res.statuses] + [OTHER]))


def build_data(res: RunResult, terms: list[str], site_url: str | None = None) -> dict:
    today = res.today or date.today()
    section_of = {s.name: s.section or OTHER for s in res.statuses}
    meetings: "OrderedDict[str, dict]" = OrderedDict()
    for r in res.window_matches:
        mid = _meeting_id(r["source"], r["body"] or "", r["meeting_date"])
        m = meetings.get(mid)
        if m is None:
            md = r["meeting_date"]
            m = meetings[mid] = {
                "id": mid,
                "source": r["source"],
                "section": section_of.get(r["source"], OTHER),
                "city": r["city"],
                "body": r["body"] or "Meeting",
                "date": md,
                "upcoming": md is None or md >= today.isoformat(),
                "documents": OrderedDict(),
                "matches": [],
            }
        m["documents"].setdefault(r["url"], {
            "title": r["title"], "kind": r["kind"], "url": r["url"], "detail_url": r["detail_url"],
        })
        m["matches"].append({
            "term": r["term"],
            "page": r["page"],
            "before": r["before"],
            "hit": r["hit"],
            "after": r["after"],
            "first_seen": r["first_seen"],
            "new": r["run_id"] == res.run_id,
            "doc": r["url"],
            "link": _page_link(r["url"], r["page"], r["kind"]),
        })
    out = []
    for m in meetings.values():
        m["documents"] = list(m["documents"].values())
        m["terms"] = sorted({x["term"] for x in m["matches"]})
        m["first_seen"] = min(x["first_seen"] for x in m["matches"])
        m["new"] = any(x["new"] for x in m["matches"])
        out.append(m)
    sources = [{
        "name": s.name, "city": s.city, "section": s.section or OTHER, "platform": s.platform,
        "listed": s.listed, "read": s.read, "unchanged": s.unchanged, "skipped": s.skipped,
        "failed": s.failed, "new_matches": s.new_matches, "error": s.error, "notes": s.notes[:5],
    } for s in res.statuses]
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "run": {"id": res.run_id, "started": res.run_started, "previous": res.previous_run},
        "today": today.isoformat(),
        "window": {"start": res.start.isoformat(), "end": res.end.isoformat()},
        "terms": terms,
        "sections": section_order(res),
        "site_url": (site_url or "").rstrip("/") or None,
        "meetings": out,
        "sources": sources,
    }


# ---------------------------------------------------------------- Atom feed
def _local_tz():
    return datetime.now().astimezone().tzinfo


def _rfc3339(ts: str) -> str:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tz())
    return dt.isoformat(timespec="seconds")


def render_atom(data: dict, limit: int = 150) -> str:
    """One entry per meeting per day it gained new matches, newest first."""
    base = data["site_url"]
    page_url = f"{base}/" if base else "index.html"
    entries = []
    for m in data["meetings"]:
        by_day: "OrderedDict[str, list]" = OrderedDict()
        for x in sorted(m["matches"], key=lambda x: x["first_seen"]):
            by_day.setdefault(x["first_seen"][:10], []).append(x)
        for day, hits in by_day.items():
            entries.append((max(h["first_seen"] for h in hits), day, m, hits))
    entries.sort(key=lambda e: e[0], reverse=True)
    updated = _rfc3339(entries[0][0]) if entries else _rfc3339(data["generated"])
    out = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        "<title>Agenda Watch — transit items on North Texas agendas</title>",
        f"<id>{FEED_TAG}:feed</id>",
        f"<updated>{updated}</updated>",
        f'<link rel="alternate" type="text/html" href="{xml_escape(page_url)}"/>',
    ]
    if base:
        out.append(f'<link rel="self" type="application/atom+xml" href="{xml_escape(base)}/feed.xml"/>')
    out.append("<author><name>Agenda Watch</name></author>")
    for ts, day, m, hits in entries[:limit]:
        when = m["date"] or "date unknown"
        terms = ", ".join(sorted({h["term"] for h in hits}))
        title = f"{m['city']} · {m['body']} ({when}): {terms}"
        parts = [f"<p><b>{escape(m['city'])} · {escape(m['body'])}</b> — meeting {escape(when)}</p><ul>"]
        for h in hits[:20]:
            parts.append(
                f"<li><b>{escape(h['term'])}</b>: {escape(h['before'])}<mark>{escape(h['hit'])}</mark>"
                f"{escape(h['after'])} <a href=\"{escape(h['link'])}\">source</a></li>")
        parts.append("</ul>")
        parts.extend(f'<p><a href="{escape(d["url"])}">{escape(d["title"] or "Agenda")}</a></p>' for d in m["documents"])
        link = f"{page_url}#m-{m['id']}"
        out += [
            "<entry>",
            f"<title>{xml_escape(title)}</title>",
            f"<id>{FEED_TAG}:{m['id']}:{day}</id>",
            f"<updated>{_rfc3339(ts)}</updated>",
            f'<link rel="alternate" type="text/html" href="{xml_escape(link)}"/>',
            f'<content type="html">{xml_escape("".join(parts))}</content>',
            "</entry>",
        ]
    out.append("</feed>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- calendar feed
def _ics_text(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> list[str]:
    """RFC 5545: lines of at most 75 octets, continuations start with a space."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return [out[0]] + [" " + x for x in out[1:]]


def render_ics(data: dict) -> str:
    """All-day events for upcoming meetings whose agendas mention a term."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = data["site_url"]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Agenda Watch//North Texas transit agendas//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Agenda Watch — transit items",
             "X-WR-TIMEZONE:America/Chicago"]
    for m in data["meetings"]:
        if not m["date"] or not m["upcoming"]:
            continue
        d = date.fromisoformat(m["date"])
        desc = [f"Agenda mentions: {', '.join(m['terms'])}", ""]
        for h in m["matches"][:8]:
            desc.append(f"{h['term']}: …{h['before'][-80:].lstrip('…')}{h['hit']}{h['after'][:80].rstrip('…')}…")
        desc.append("")
        desc += [f"{doc['title'] or 'Agenda'}: {doc['url']}" for doc in m["documents"]]
        desc.append("Confirm details against the official posted agenda.")
        url = (f"{base}/#m-{m['id']}" if base else m["documents"][0]["url"])
        summary = "{}: {} — {}".format(m["city"], m["body"], ", ".join(m["terms"]))
        for raw in [
            "BEGIN:VEVENT",
            f"UID:{m['id']}@agendawatch",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{d:%Y%m%d}",
            f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{_ics_text(summary)}",
            f"DESCRIPTION:{_ics_text(chr(10).join(desc))}",
            f"URL:{url}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]:
            lines.extend(_fold(raw))
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------- page
_TEMPLATE = (Path(__file__).parent / "site_template.html")


def render_page(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)


def write_site(res: RunResult, terms: list[str], out_dir: str | Path, site_url: str | None = None) -> Path:
    """Write index.html, data.json, feed.xml, upcoming.ics, plus latest.html and a dated snapshot."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = build_data(res, terms, site_url)
    page = render_page(data)
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "feed.xml").write_text(render_atom(data), encoding="utf-8")
    with open(out / "upcoming.ics", "w", encoding="utf-8", newline="") as fh:
        fh.write(render_ics(data))
    shutil.copyfile(out / "index.html", out / "latest.html")
    snapshot = out / f"agenda-report-{(res.today or date.today()):%Y-%m-%d}.html"
    shutil.copyfile(out / "index.html", snapshot)
    return out / "index.html"
