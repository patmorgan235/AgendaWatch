"""Public site output: data.json, a single-page report, an Atom feed and a calendar feed.

Everything is built from one data structure (build_data), so the page, the feeds and any future
consumer agree. The page embeds its data, so it works opened from disk as well as when hosted.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from xml.sax.saxutils import escape as xml_escape

from .feeds import OTHER  # noqa: F401 - re-exported: report.py imports it from here
from .runner import RunResult

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


def taxonomy_from_config(cfg: dict) -> dict:
    """Tags per term and the action-tag patterns, from config.yaml (`terms[].tags`, `actions`)."""
    from .matcher import term_tags

    return {"tags": term_tags(cfg.get("terms") or []), "actions": dict(cfg.get("actions") or {})}


def _action_matchers(taxonomy: dict | None) -> list[tuple[str, re.Pattern]]:
    out = []
    for name, pattern in ((taxonomy or {}).get("actions") or {}).items():
        try:
            out.append((str(name), re.compile(str(pattern), re.IGNORECASE)))
        except re.error as exc:
            raise ValueError(f"Action '{name}' has an invalid pattern: {exc}") from exc
    return out


def build_data(res: RunResult, terms: list[str], site_url: str | None = None, taxonomy: dict | None = None) -> dict:
    """`taxonomy` (see taxonomy_from_config) adds tags and action tags to each mention. Tags are
    applied here, when the report is built, so changing them in the config re-tags everything."""
    today = res.today or date.today()
    tag_map = (taxonomy or {}).get("tags") or {}
    action_matchers = _action_matchers(taxonomy)
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
            "tags": list(tag_map.get(r["term"], [])),
            "actions": [name for name, rx in action_matchers
                        if rx.search(f"{r['before']} {r['hit']} {r['after']}")],
        })
    base = (site_url or "").rstrip("/") or None
    out = []
    for m in meetings.values():
        m["documents"] = list(m["documents"].values())
        m["terms"] = sorted({x["term"] for x in m["matches"]})
        m["tags"] = sorted({t for x in m["matches"] for t in x["tags"]})
        m["actions"] = sorted({a for x in m["matches"] for a in x["actions"]})
        m["first_seen"] = min(x["first_seen"] for x in m["matches"])
        m["new"] = any(x["new"] for x in m["matches"])
        if m["date"]:
            m["add"] = {"ics": meeting_ics_path(m), "google": google_add_url(m, base)}
        out.append(m)
    sources = [{
        "name": s.name, "city": s.city, "section": s.section or OTHER, "platform": s.platform,
        "listed": s.listed, "read": s.read, "unchanged": s.unchanged, "skipped": s.skipped,
        "failed": s.failed, "new_matches": s.new_matches, "error": s.error, "notes": s.notes[:5],
        "last_new": s.last_new, "stale": s.stale, "stale_after_days": s.stale_after_days,
        "lead_days": s.lead_days, "lead_samples": s.lead_samples,
        "failing_runs": s.failing_runs, "failing_since": s.failing_since,
    } for s in res.statuses]
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "run": {"id": res.run_id, "started": res.run_started, "previous": res.previous_run},
        "today": today.isoformat(),
        "window": {"start": (res.report_start or res.start).isoformat(), "end": res.end.isoformat()},
        "terms": terms,
        # Tag groups (agency, mode, topic …) and action tags present in this report, for the filters.
        "taxonomy": {
            "groups": {g: sorted({t.split(":", 1)[1] for m in out for t in m["tags"] if t.split(":", 1)[0] == g})
                       for g in dict.fromkeys(t.split(":", 1)[0] for v in tag_map.values() for t in v)},
            "actions": [name for name, _ in action_matchers],
        },
        "sections": section_order(res),
        "site_url": base,
        "meetings": out,
        "sources": sources,
        "feeds": feed_list(res, base),
    }


# ---------------------------------------------------------------- per-section / per-city feeds
def _feed_entry(f: dict, base: str | None) -> dict:
    path = f["path"]
    feed = {"kind": f["kind"], "name": f["name"], "slug": path.rsplit("/", 1)[-1],
            "ics": f"{path}.ics", "atom": f"{path}.xml"}
    if base:   # Google needs an absolute address to subscribe to
        host_path = base.split("://", 1)[-1]
        feed["google"] = "https://calendar.google.com/calendar/r?" + urlencode(
            {"cid": f"webcal://{host_path}/{path}.ics"})
    return feed


def feed_list(res: RunResult, base: str | None) -> list[dict]:
    """A calendar and an Atom feed for every watched city and report section.

    The names and their (stable) paths come from the run (feeds.py): every configured source's
    city and section, plus any seen in the report window's meetings. A feed exists, empty, even
    when its city has no mentions right now, so subscription links never break."""
    return [_feed_entry(f, base) for f in res.feeds]


def feed_meetings(data: dict, feed: dict) -> list[dict]:
    key = "section" if feed["kind"] == "section" else "city"
    return [m for m in data["meetings"] if m[key] == feed["name"]]


RETIRED_NOTE = "{name} is no longer watched, so this feed is no longer updated. See {page} for current feeds."


def write_feeds(out: Path, data: dict, retired: list[dict] | None = None) -> None:
    for feed in data["feeds"]:
        meetings = feed_meetings(data, feed)
        _write_ics(out / feed["ics"], render_ics(data, meetings, name=f"Agenda Watch — {feed['name']}"))
        (out / feed["atom"]).write_text(render_atom(
            data, meetings=meetings, title=f"Agenda Watch — {feed['name']}: transit items",
            self_path=feed["atom"], feed_id=f"feed:{feed['kind']}:{feed['slug']}",
            page_query="?" + urlencode({feed["kind"]: feed["name"]})), encoding="utf-8")
    # Feeds published before whose city or section is gone: empty them, with a notice, rather
    # than leave their last events up for subscribers forever.
    for f in retired or []:
        feed = _feed_entry(f, data["site_url"])
        note = RETIRED_NOTE.format(name=f["name"], page=data["site_url"] or "the Agenda Watch page")
        _write_ics(out / feed["ics"], render_ics(data, [], name=f"Agenda Watch — {f['name']} (no longer updated)",
                                                 description=note))
        (out / feed["atom"]).write_text(render_atom(
            data, meetings=[], title=f"Agenda Watch — {f['name']}: no longer updated",
            self_path=feed["atom"], feed_id=f"feed:{feed['kind']}:{feed['slug']}",
            notice=(note, f.get("last_published") or data["generated"])), encoding="utf-8")


# ---------------------------------------------------------------- Atom feed
def _local_tz():
    return datetime.now().astimezone().tzinfo


def _rfc3339(ts: str) -> str:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tz())
    return dt.isoformat(timespec="seconds")


def render_atom(data: dict, limit: int = 150, meetings: list[dict] | None = None,
                title: str = "Agenda Watch — transit items on North Texas agendas",
                self_path: str = "feed.xml", feed_id: str = "feed", page_query: str = "",
                notice: tuple[str, str] | None = None) -> str:
    """One entry per meeting per day it gained new matches, newest first.

    `notice` = (text, timestamp) adds one fixed entry, e.g. to say a feed is no longer updated."""
    base = data["site_url"]
    # Without site_url, links are relative to the feed's own folder (feeds/city/x.xml → ../../).
    page_url = f"{base}/" if base else "../" * self_path.count("/") + "index.html"
    entries = []
    for m in data["meetings"] if meetings is None else meetings:
        by_day: "OrderedDict[str, list]" = OrderedDict()
        for x in sorted(m["matches"], key=lambda x: x["first_seen"]):
            by_day.setdefault(x["first_seen"][:10], []).append(x)
        for day, hits in by_day.items():
            entries.append((max(h["first_seen"] for h in hits), day, m, hits))
    entries.sort(key=lambda e: e[0], reverse=True)
    updated = _rfc3339(entries[0][0]) if entries else _rfc3339(notice[1] if notice else data["generated"])
    out = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"<title>{xml_escape(title)}</title>",
        f"<id>{FEED_TAG}:{feed_id}</id>",
        f"<updated>{updated}</updated>",
        f'<link rel="alternate" type="text/html" href="{xml_escape(page_url + page_query)}"/>',
    ]
    if base:
        out.append(f'<link rel="self" type="application/atom+xml" href="{xml_escape(base)}/{xml_escape(self_path)}"/>')
    out.append("<author><name>Agenda Watch</name></author>")
    if notice:
        # A fixed id and timestamp, so readers show it once rather than as new on every run.
        out += [
            "<entry>",
            f"<title>{xml_escape(notice[0])}</title>",
            f"<id>{FEED_TAG}:{feed_id}:notice</id>",
            f"<updated>{_rfc3339(notice[1])}</updated>",
            f'<link rel="alternate" type="text/html" href="{xml_escape(page_url)}"/>',
            f"<content type=\"text\">{xml_escape(notice[0])}</content>",
            "</entry>",
        ]
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


def _ics_utc(ts: str) -> str:
    """A stored timestamp (local time unless it says otherwise) as an iCalendar UTC time."""
    return datetime.fromisoformat(_rfc3339(ts)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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


def event_text(m: dict, base: str | None) -> tuple[str, str, str]:
    """(summary, description, url) for a meeting as a calendar event. Shared by the calendar
    feeds, the per-meeting .ics files, the "Add to Google Calendar" link and the Google push."""
    desc = [f"Agenda mentions: {', '.join(m['terms'])}", ""]
    for h in m["matches"][:8]:
        desc.append(f"{h['term']}: …{h['before'][-80:].lstrip('…')}{h['hit']}{h['after'][:80].rstrip('…')}…")
    desc.append("")
    desc += [f"{doc['title'] or 'Agenda'}: {doc['url']}" for doc in m["documents"]]
    desc.append("Confirm details against the official posted agenda.")
    url = f"{base}/#m-{m['id']}" if base else m["documents"][0]["url"]
    summary = "{}: {} — {}".format(m["city"], m["body"], ", ".join(m["terms"]))
    return summary, "\n".join(desc), url


def meeting_ics_path(m: dict) -> str:
    return f"feeds/meeting/{m['id']}.ics"


GOOGLE_DETAILS_MAX = 1500   # keep the link well under URL length limits


def google_add_url(m: dict, base: str | None) -> str:
    """A link that opens Google Calendar's "new event" form, prefilled with this meeting."""
    summary, desc, url = event_text(m, base)
    if base:
        desc = f"{url}\n\n{desc}"
    if len(desc) > GOOGLE_DETAILS_MAX:
        desc = desc[:GOOGLE_DETAILS_MAX - 1] + "…"
    d = date.fromisoformat(m["date"])
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": summary, "details": desc,
        "dates": f"{d:%Y%m%d}/{d + timedelta(days=1):%Y%m%d}",
    })


def render_ics(data: dict, meetings: list[dict] | None = None, name: str = "Agenda Watch — transit items",
               include_past: bool = False, description: str | None = None) -> str:
    """All-day events for meetings whose agendas mention a term (by default every upcoming one)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = data["site_url"]
    lines: list[str] = []
    header = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Agenda Watch//North Texas transit agendas//EN",
              "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_text(name)}",
              "X-WR-TIMEZONE:America/Chicago",
              # ask subscribing apps to check twice a day (the site updates daily)
              "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H"]
    if description:
        header.append(f"X-WR-CALDESC:{_ics_text(description)}")
    for raw in header:
        lines.extend(_fold(raw))
    for m in data["meetings"] if meetings is None else meetings:
        if not m["date"] or not (m["upcoming"] or include_past):
            continue
        d = date.fromisoformat(m["date"])
        summary, desc, url = event_text(m, base)
        for raw in [
            "BEGIN:VEVENT",
            f"UID:{m['id']}@agendawatch",
            f"DTSTAMP:{stamp}",
            f"LAST-MODIFIED:{_ics_utc(max(x['first_seen'] for x in m['matches']))}",
            f"DTSTART;VALUE=DATE:{d:%Y%m%d}",
            f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{_ics_text(summary)}",
            f"DESCRIPTION:{_ics_text(desc)}",
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


def _write_ics(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:   # keep the CRLF line endings
        fh.write(text)


def write_site(res: RunResult, terms: list[str], out_dir: str | Path, site_url: str | None = None,
               taxonomy: dict | None = None) -> Path:
    """Write index.html, data.json, feed.xml, upcoming.ics, plus latest.html and a dated snapshot."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = build_data(res, terms, site_url, taxonomy)
    page = render_page(data)
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "feed.xml").write_text(render_atom(data), encoding="utf-8")
    _write_ics(out / "upcoming.ics", render_ics(data))
    write_feeds(out, data, res.retired_feeds)
    # One file per meeting, for "Add to calendar → .ics". Old meetings' files are left in place:
    # dated snapshot pages still link to them.
    for m in data["meetings"]:
        if m.get("add"):
            name = f"Agenda Watch — {m['city']} {m['body']}"
            _write_ics(out / meeting_ics_path(m), render_ics(data, [m], name=name, include_past=True))
    shutil.copyfile(out / "index.html", out / "latest.html")
    snapshot = out / f"agenda-report-{(res.today or date.today()):%Y-%m-%d}.html"
    shutil.copyfile(out / "index.html", snapshot)
    return out / "index.html"
