"""Swagit video archives, archive matches (not news), and partial runs (--only)."""

from datetime import date

import pytest

from agendawatch.runner import run, select_sources
from agendawatch.sources import SourceError, SwagitSource

from helpers import FakeHttp, make_pdf

BASE = "https://citytx.new.swagit.com"


def row(vid, title, when, agenda=True, commented=False, nbsp=False):
    link = f'<a target="_blank" href="/videos/{vid}/agenda">Agenda</a>' if agenda else ""
    if commented:
        link = f"<!-- <td> {link} </td> -->"
    return (f'<tr><td><a target="_blank" href="/videos/{vid}">{title}</a>{"&nbsp;" if nbsp else ""}</td>'
            f"<td nowrap> {when} </td><td>1h</td>{link}</tr>")


def page(*rows, more=False):
    pager = '<a href="?page=2">2</a>' if more else ""
    return f'<table id="video-table"><tbody>{"".join(rows)}</tbody></table>{pager}'


def source(http, **kw):
    return SwagitSource(name="City (video archive)", city="City", http=http, host="citytx", view=9, **kw)


def test_reads_rows_agendas_in_comments_and_skips_videos_without_agendas():
    http = FakeHttp({f"{BASE}/views/9": page(
        row(3, "City Council", "Jul 10, 2025", commented=True),
        row(2, "City Council Work Session", "Jun 02, 2025", nbsp=True),
        row(1, "Ribbon Cutting", "May 01, 2025", agenda=False))})
    docs = source(http).list_documents(date(2025, 1, 1), date(2025, 12, 31))
    assert [(d.body, d.meeting_date, d.url) for d in docs] == [
        ("City Council", date(2025, 7, 10), f"{BASE}/videos/3/agenda"),
        ("City Council Work Session", date(2025, 6, 2), f"{BASE}/videos/2/agenda")]
    assert docs[0].detail_url == f"{BASE}/videos/3"


def test_pages_until_older_than_the_window():
    http = FakeHttp({
        f"{BASE}/views/9/meetings": page(row(30, "City Council", "Mar 01, 2025"), more=True),
        f"{BASE}/views/9/meetings?page=2": page(row(20, "City Council", "Jan 05, 2024"), more=True),
        f"{BASE}/views/9/meetings?page=3": page(row(10, "City Council", "Jun 01, 2022"), more=True),
        f"{BASE}/views/9/meetings?page=4": page(row(5, "City Council", "Jan 01, 2020"), more=True),
    })
    docs = source(http, tab="meetings").list_documents(date(2023, 1, 1), date(2025, 12, 31))
    assert sorted(d.meeting_date.year for d in docs) == [2024, 2025]
    assert f"{BASE}/views/9/meetings?page=4" not in http.calls    # stopped after the first page fully outside


def test_until_include_and_exclude():
    http = FakeHttp({f"{BASE}/views/9": page(
        row(4, "City Council", "Nov 01, 2026"),
        row(3, "City Council", "Oct 01, 2026"),
        row(2, "Planning Commission", "Sep 01, 2026"),
        row(1, "Council Retreat", "Aug 01, 2026"))})
    docs = source(http, until="2026-10-15", include="council", exclude="retreat").list_documents(
        date(2026, 1, 1), date(2026, 12, 31))
    assert [d.meeting_date for d in docs] == [date(2026, 10, 1)]


def test_empty_archive_is_an_error():
    with pytest.raises(SourceError):
        source(FakeHttp({f"{BASE}/views/9": "<html>nothing</html>"})).list_documents(date(2025, 1, 1), date(2025, 12, 31))


def cfg(tmp_path):
    return {
        "settings": {"database": str(tmp_path / "t.db"), "report_dir": str(tmp_path / "r"),
                     "request_delay_seconds": 0, "lookback_days": 10, "lookahead_days": 60, "report_lookback_days": 30},
        "terms": [{"name": "DART", "case_sensitive": True}],
        "sources": [
            {"name": "City (video archive)", "city": "City", "type": "swagit", "host": "citytx", "view": 9},
            {"name": "Other", "type": "link_page", "urls": ["https://other.example/agendas"], "link_pattern": r"\.pdf"},
        ],
    }


def test_archive_matches_are_stored_but_not_news(tmp_path):
    today = date(2026, 11, 23)
    http = FakeHttp({
        f"{BASE}/views/9": page(row(2, "City Council", "Nov 18, 2026"), row(1, "City Council", "Mar 03, 2022")),
        f"{BASE}/videos/2/agenda": make_pdf(["Item 4. DART bus stop agreement"]),
        f"{BASE}/videos/1/agenda": make_pdf(["Item 7. DART interlocal agreement"]),
    })
    res = run(cfg(tmp_path), backfill_days=2000, http=http, today=today, only=["City (video archive)"])
    assert [s.name for s in res.statuses] == ["City (video archive)"]       # --only
    assert {r["meeting_date"] for r in res.new_matches} == {"2026-11-18"}   # recent: news
    from agendawatch.store import Store
    old = Store(tmp_path / "t.db").db.execute(
        "SELECT m.run_id, m.first_seen FROM matches m JOIN documents d ON d.url=m.url WHERE d.meeting_date='2022-03-03'").fetchall()
    assert [tuple(r) for r in old] == [(0, "2022-03-03T00:00:00")]          # archive: stored, back-dated


def test_only_rejects_unknown_names():
    with pytest.raises(ValueError, match="No source named"):
        select_sources([{"name": "A"}], ["B"])


def test_archive_is_written_per_year_and_indexed(tmp_path):
    import json

    from agendawatch.report import write_report
    from agendawatch.site import taxonomy_from_config

    today = date(2026, 11, 23)
    c = cfg(tmp_path)
    c["terms"] = [{"name": "DART", "case_sensitive": True, "tags": ["agency:DART"]},
                  {"name": "Silver Line", "tags": ["mode:rail"]}]
    http = FakeHttp({
        f"{BASE}/views/9": page(row(3, "City Council", "Nov 18, 2026"), row(2, "City Council", "Dec 12, 2022"),
                                row(1, "City Council", "Mar 03, 2022")),
        f"{BASE}/videos/3/agenda": make_pdf(["Item 4. DART bus stop agreement"]),
        f"{BASE}/videos/2/agenda": make_pdf(["Item 2. Silver Line alignment resolution"]),
        f"{BASE}/videos/1/agenda": make_pdf(["Item 7. DART interlocal agreement"]),
    })
    run(c, backfill_days=2000, http=http, today=today, only=["City (video archive)"])   # the archive backfill
    c["sources"] = c["sources"][:1]
    res = run(c, http=http, today=today)                                               # then a normal daily run
    out = tmp_path / "site"
    write_report(res, ["DART", "Silver Line"], out, taxonomy=taxonomy_from_config(c))
    data = json.loads((out / "data.json").read_text())
    assert [m["date"] for m in data["meetings"]] == ["2026-11-18"]               # the page: recent only
    idx = json.loads((out / "archive/index.json").read_text())
    assert idx == data["archive"]
    assert [(y["year"], y["meetings"]) for y in idx["years"]] == [("2022", 2)]
    assert idx["cities"]["City"] == {"meetings": 2, "since": "2022-03-03", "years": ["2022"]}
    year = json.loads((out / "archive/2022.json").read_text())
    assert [m["date"] for m in year["meetings"]] == ["2022-12-12", "2022-03-03"]   # newest first
    assert all(not m["new"] and "add" not in m for m in year["meetings"])
    assert "rail" in data["taxonomy"]["groups"]["mode"]                         # archive-only tag still filterable


def test_archive_sources_run_only_when_named():
    specs = [{"name": "A"}, {"name": "B (video archive)", "archive": True}]
    assert [s["name"] for s in select_sources(specs, None)] == ["A"]
    assert [s["name"] for s in select_sources(specs, ["B (video archive)"])] == ["B (video archive)"]


def test_several_tabs():
    http = FakeHttp({
        f"{BASE}/views/9/city-council": page(row(2, "City Council", "Mar 01, 2025")),
        f"{BASE}/views/9/planning-and-zoning": page(row(1, "Planning and Zoning", "Feb 01, 2025")),
    })
    s = source(http, tab=["city-council", "planning-and-zoning"])
    assert sorted(d.body for d in s.list_documents(date(2025, 1, 1), date(2025, 12, 31))) == ["City Council", "Planning and Zoning"]
    assert s.page_url() == f"{BASE}/views/9/city-council"
