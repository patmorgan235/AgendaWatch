"""Daily report: a self-contained HTML page plus a short plain-text summary."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
from html import escape
from pathlib import Path

from .runner import RunResult


def _safe_fmt_date(iso: str | None) -> str:
    if not iso:
        return "Date unknown"
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d:%a %b} {d.day}, {d.year}"  # avoids %-d, which Windows doesn't support


def _page_link(url: str, page: int, kind: str) -> str:
    if kind in ("items",) or "#" in url:
        return escape(url)
    return escape(f"{url}#page={page}")


CSS = """
:root{--bg:#f7f6f2;--card:#ffffff;--ink:#1d2327;--muted:#5d6670;--line:#e3e0d8;--accent:#0b6e4f;
--mark:#ffe58a;--markink:#1d2327;--chip:#e8f1ec;--chipink:#0b5a41;--warn:#9a3412;--warnbg:#fff1e6}
@media (prefers-color-scheme: dark){:root{--bg:#14171a;--card:#1c2126;--ink:#e7eaed;--muted:#9aa4ae;
--line:#2c333a;--accent:#4fc39a;--mark:#6b5a12;--markink:#fff6cf;--chip:#1f3a30;--chipink:#9fe3c6;
--warn:#fdba74;--warnbg:#3a2414}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:28px 16px 64px}
header h1{font-size:1.6rem;margin:0 0 4px;letter-spacing:-.01em}
header p{margin:0;color:var(--muted)}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 8px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:120px}
.stat b{display:block;font-size:1.4rem;line-height:1.2}
.stat span{color:var(--muted);font-size:.85rem}
h2{font-size:1.15rem;margin:34px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--line)}
.meeting{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:0 0 14px}
.meeting h3{margin:0;font-size:1.05rem}
.meta{color:var(--muted);font-size:.9rem;margin:2px 0 10px}
.meta a{color:var(--accent)}
.hit{border-top:1px solid var(--line);padding:10px 0 4px}
.hit:first-of-type{border-top:0}
.chip{display:inline-block;background:var(--chip);color:var(--chipink);border-radius:999px;padding:1px 10px;font-size:.8rem;font-weight:600;margin-right:6px}
.page{font-size:.82rem;color:var(--accent);text-decoration:none}
.snip{margin:6px 0 0;color:var(--ink)}
mark{background:var(--mark);color:var(--markink);padding:0 2px;border-radius:3px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:.92rem}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:.82rem;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:0}
td a{color:var(--accent)}
.empty{background:var(--card);border:1px dashed var(--line);border-radius:12px;padding:18px;color:var(--muted)}
.err{color:var(--warn)}
.note{color:var(--muted);font-size:.85rem}
.tablewrap{overflow-x:auto}
footer{margin-top:40px;color:var(--muted);font-size:.82rem}
"""


def build_html(res: RunResult, terms: list[str]) -> str:
    generated = datetime.now()
    groups: "OrderedDict[str, list]" = OrderedDict()
    for row in res.new_matches:
        groups.setdefault(row["url"], []).append(row)

    meetings_with_new = len(groups)
    errors = sum(1 for s in res.statuses if s.error)
    checked = sum(s.listed for s in res.statuses)

    out = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Agenda Watch · {generated:%b %d, %Y}</title><style>{CSS}</style></head><body><div class='wrap'>",
        "<header><h1>Agenda Watch</h1>",
        f"<p>{escape(generated.strftime('%A, %B %d, %Y · %I:%M %p'))} · meetings from "
        f"{res.start:%b %d} to {res.end:%b %d, %Y}</p>",
        f"<p class='note'>Watching for: {escape(', '.join(terms))}</p></header>",
        "<div class='stats'>",
        f"<div class='stat'><b>{len(res.new_matches)}</b><span>new mentions</span></div>",
        f"<div class='stat'><b>{meetings_with_new}</b><span>agendas with new mentions</span></div>",
        f"<div class='stat'><b>{len(res.upcoming)}</b><span>upcoming agendas with mentions</span></div>",
        f"<div class='stat'><b>{checked}</b><span>agendas checked</span></div>",
        "</div>",
    ]
    if errors:
        out.append(f"<p class='err'>{errors} source(s) couldn't be read today — see Sources below.</p>")

    since = f" since {escape(res.previous_run[:16].replace('T', ' '))}" if res.previous_run else ""
    out.append(f"<h2>New mentions{since}</h2>")
    if not groups:
        out.append("<div class='empty'>No new mentions of your terms today.</div>")
    for url, rows in groups.items():
        r0 = rows[0]
        links = [f"<a href='{escape(url)}'>Open agenda</a>"] if r0["kind"] != "items" else []
        if r0["detail_url"]:
            links.append(f"<a href='{escape(r0['detail_url'])}'>Meeting page</a>")
        out.append("<section class='meeting'>")
        out.append(f"<h3>{escape(r0['city'])} · {escape(r0['body'] or 'Meeting')}</h3>")
        out.append(f"<div class='meta'>{escape(_safe_fmt_date(r0['meeting_date']))}"
                   f"{' · ' + ' · '.join(links) if links else ''}</div>")
        for r in rows:
            page = (f"<a class='page' href='{_page_link(url, r['page'], r['kind'])}'>page {r['page']}</a>"
                    if r["kind"] != "items" else "")
            out.append(
                "<div class='hit'>"
                f"<span class='chip'>{escape(r['term'])}</span>{page}"
                f"<p class='snip'>{escape(r['before'])}<mark>{escape(r['hit'])}</mark>{escape(r['after'])}</p>"
                "</div>"
            )
        out.append("</section>")

    out.append("<h2>Upcoming meetings that mention your terms</h2>")
    if not res.upcoming:
        out.append("<div class='empty'>None of the posted upcoming agendas mention your terms yet.</div>")
    else:
        out.append("<div class='tablewrap'><table><tr><th>Date</th><th>City</th><th>Meeting</th><th>Terms</th><th></th></tr>")
        for u in res.upcoming:
            link = u["detail_url"] if u["kind"] == "items" and u["detail_url"] else u["url"]
            out.append(
                f"<tr><td>{escape(_safe_fmt_date(u['meeting_date']))}</td><td>{escape(u['city'])}</td>"
                f"<td>{escape(u['body'] or '')}</td><td>{escape((u['terms'] or '').replace(',', ', '))}</td>"
                f"<td><a href='{escape(link)}'>Agenda</a></td></tr>"
            )
        out.append("</table></div>")

    out.append("<h2>Sources</h2><div class='tablewrap'><table><tr><th>Source</th><th>Platform</th>"
               "<th>Agendas</th><th>Read</th><th>New mentions</th><th>Status</th></tr>")
    for s in res.statuses:
        if s.error:
            status = f"<span class='err'>{escape(s.error[:220])}</span>"
        else:
            bits = []
            if s.unchanged:
                bits.append(f"{s.unchanged} unchanged")
            if s.skipped:
                bits.append(f"{s.skipped} already read")
            if s.failed:
                bits.append(f"<span class='err'>{s.failed} failed to download</span>")
            status = ", ".join(bits) or "OK"
        if s.notes:
            status += "".join(f"<div class='note'>{escape(n[:240])}</div>" for n in s.notes[:4])
        out.append(f"<tr><td>{escape(s.name)}</td><td>{escape(s.platform)}</td><td>{s.listed}</td>"
                   f"<td>{s.read}</td><td>{s.new_matches}</td><td>{status}</td></tr>")
    out.append("</table></div>")
    out.append("<footer>Generated by agenda-watch. Page links open the agenda PDF at the matching page "
               "in most browsers. Always confirm against the official posted agenda.</footer>")
    out.append("</div></body></html>")
    return "\n".join(out)


def write_report(res: RunResult, terms: list[str], report_dir: str | Path) -> Path:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    html = build_html(res, terms)
    path = report_dir / f"agenda-report-{date.today():%Y-%m-%d}.html"
    path.write_text(html, encoding="utf-8")
    (report_dir / "latest.html").write_text(html, encoding="utf-8")
    return path


def text_summary(res: RunResult) -> str:
    lines = [f"Agenda Watch — {date.today():%Y-%m-%d}"]
    if not res.new_matches:
        lines.append("No new mentions today.")
    else:
        seen = OrderedDict()
        for r in res.new_matches:
            k = (r["city"], r["body"], r["meeting_date"])
            seen.setdefault(k, set()).add(r["term"])
        lines.append(f"{len(res.new_matches)} new mention(s) in {len(seen)} agenda(s):")
        for (city, body, d), terms in seen.items():
            lines.append(f"  • {d or '????-??-??'}  {city} — {body}: {', '.join(sorted(terms))}")
    for s in res.statuses:
        if s.error:
            lines.append(f"  ! {s.name}: {s.error[:160]}")
    return "\n".join(lines)
