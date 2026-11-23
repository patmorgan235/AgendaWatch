"""Report output: the public site (see site.py) and a short plain-text summary for the console."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
from pathlib import Path

from .runner import RunResult
from .site import OTHER, build_data, render_page, section_order, write_site


def source_sections(res: RunResult) -> dict[str, str]:
    """Source name → report section, from today's run (config order)."""
    return {s.name: s.section or OTHER for s in res.statuses}


def build_html(res: RunResult, terms: list[str], site_url: str | None = None, taxonomy: dict | None = None) -> str:
    """The report page, with its data embedded (works opened from disk)."""
    return render_page(build_data(res, terms, site_url, taxonomy))


def write_report(res: RunResult, terms: list[str], report_dir: str | Path, site_url: str | None = None,
                 taxonomy: dict | None = None) -> Path:
    """Write the site into report_dir: index.html (= latest.html), data.json, feed.xml,
    upcoming.ics and a dated snapshot. Returns the path of index.html."""
    return write_site(res, terms, report_dir, site_url, taxonomy)


def text_summary(res: RunResult) -> str:
    lines = [f"Agenda Watch — {date.today():%Y-%m-%d}"]
    if not res.new_matches:
        lines.append("No new mentions today.")
    else:
        section_of = source_sections(res)
        by_section: "OrderedDict[str, OrderedDict]" = OrderedDict((sec, OrderedDict()) for sec in section_order(res))
        for r in res.new_matches:
            k = (r["city"], r["body"], r["meeting_date"])
            by_section.setdefault(section_of.get(r["source"], OTHER), OrderedDict()).setdefault(k, set()).add(r["term"])
        total = sum(len(v) for v in by_section.values())
        lines.append(f"{len(res.new_matches)} new mention(s) in {total} agenda(s):")
        for sec, seen in by_section.items():
            if not seen:
                continue
            lines.append(f"  {sec}")
            for (city, body, d), terms in seen.items():
                lines.append(f"    • {d or '????-??-??'}  {city} — {body}: {', '.join(sorted(terms))}")
    for s in res.statuses:
        if s.error:
            lines.append(f"  ! {s.name}: {s.error[:160]}")
    return "\n".join(lines)
