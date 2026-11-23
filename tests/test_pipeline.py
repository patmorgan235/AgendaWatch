"""End-to-end: list → download → extract → match → dedupe → report."""

import json
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import yaml

from agendawatch.http import FetchError
from agendawatch.report import build_html, text_summary, write_report
from agendawatch.site import build_data, render_atom, render_ics
from agendawatch.runner import run

from helpers import FakeHttp, make_pdf

TODAY = date(2026, 10, 31)
ROOT = Path(__file__).parent.parent


def config(tmp_path):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["settings"].update(database=str(tmp_path / "t.db"), report_dir=str(tmp_path / "r"), request_delay_seconds=0)
    cfg["sources"] = [
        {"name": "Dallas", "type": "legistar", "client": "cityofdallas", "section": "DART member cities"},
        {"name": "Plano", "type": "novus", "base_url": "https://plano.novusagenda.com/agendapublic",
         "section": "DCTA member cities"},  # stands in for a DCTA city in these tests
        {"name": "Addison", "type": "link_page", "urls": ["https://agendas.addisontx.gov/agenda_publish.cfm"],
         "link_pattern": r"\.pdf", "section": "DART member cities"},
    ]
    return cfg


AGENDA_P1 = "CITY COUNCIL AGENDA NOVEMBER 4, 2026\nItem 1. Approve minutes\nItem 2. Parks report"
AGENDA_P2 = ("Item 14. Authorize an interlocal agreement with Dallas Area Rapid Transit (DART)\n"
             "for General Mobility Program (GMP) funds for the Elm Street streetcar study.")
CONSTRUCTION = "Item 9. Approve a GMP amendment (Guaranteed Maximum Price) for the library."
PARK_PDF = make_pdf([CONSTRUCTION])
PLANO_PDF = make_pdf(["Presentation on the DART withdrawal election results."])


def routes(dallas_pdf):
    return {
        "https://webapi.legistar.com/v1/cityofdallas/events": [
            {"EventId": 1, "EventBodyName": "Transportation and Infrastructure Committee",
             "EventDate": "2026-11-04T00:00:00", "EventAgendaFile": "https://d/agenda1.pdf",
             "EventInSiteURL": "https://cityofdallas.legistar.com/MeetingDetail.aspx?ID=1"},
            {"EventId": 2, "EventBodyName": "Park Board", "EventDate": "2026-11-05T00:00:00",
             "EventAgendaFile": "https://d/agenda2.pdf"},
        ],
        "https://d/agenda1.pdf": dallas_pdf,
        "https://d/agenda2.pdf": PARK_PDF,
        "https://plano.novusagenda.com/agendapublic/MeetingsResponsive.aspx":
            '<tr><td>11/09/26</td><td>City Council</td>'
            '<td><a href="DisplayAgendaPDF.ashx?MeetingID=5">PDF</a></td></tr>',
        "https://plano.novusagenda.com/agendapublic/DisplayAgendaPDF.ashx?MeetingID=5":
            PLANO_PDF,
        "https://agendas.addisontx.gov/agenda_publish.cfm": FetchError("HTTP 503 for addison"),
    }


def test_full_run_dedupes_and_reports(tmp_path):
    cfg = config(tmp_path)
    pdf = make_pdf([AGENDA_P1, AGENDA_P2])

    res = run(cfg, http=FakeHttp(routes(pdf)), today=TODAY)
    terms = {r["term"] for r in res.new_matches}
    # The full names are aliases ("Dallas Area Rapid Transit" counts as DART, "General Mobility
    # Program" as GMP), and the "Transit" inside the full name isn't counted separately.
    # (Plano's "DART withdrawal election" is caught by the DART membership term too)
    assert terms == {"DART", "GMP", "Streetcar", "DART membership"}
    assert all(r["page"] == 2 for r in res.new_matches if r["url"] == "https://d/agenda1.pdf")
    # GMP in the Park Board construction item is filtered out as "Guaranteed Maximum Price"
    assert not [r for r in res.new_matches if r["url"] == "https://d/agenda2.pdf"]
    by_status = {s.name: s for s in res.statuses}
    assert by_status["Plano"].new_matches == 1
    assert "503" in by_status["Addison"].error  # one broken site doesn't stop the run
    assert len(res.upcoming) == 2

    data = build_data(res, ["DART"])
    dallas = next(m for m in data["meetings"] if m["city"] == "Dallas")
    assert dallas["body"] == "Transportation and Infrastructure Committee" and dallas["section"] == "DART member cities"
    assert dallas["upcoming"] and all(x["new"] for x in dallas["matches"])
    # "Dallas Area Rapid Transit (DART)" on one line is one DART passage, not two
    dart = [x for x in dallas["matches"] if x["term"] == "DART"]
    assert len(dart) == 1 and dart[0]["link"] == "https://d/agenda1.pdf#page=2"
    assert next(m for m in data["meetings"] if m["city"] == "Plano")["section"] == "DCTA member cities"
    assert data["sections"][:2] == ["DART member cities", "DCTA member cities"]
    assert "503" in next(s for s in data["sources"] if s["name"] == "Addison")["error"]

    html = build_html(res, ["DART"])
    embedded = html.split('<script id="data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(embedded)["meetings"][0]["id"] == data["meetings"][0]["id"]

    summary = text_summary(res)
    assert "new mention" in summary
    assert summary.index("DART member cities") < summary.index("Dallas —") < summary.index("DCTA member cities") < summary.index("Plano —")

    # Second run, nothing changed: no new matches, nothing re-extracted.
    res2 = run(cfg, http=FakeHttp(routes(pdf)), today=TODAY)
    assert res2.new_matches == []
    assert {s.name: s.unchanged for s in res2.statuses}["Dallas"] == 2
    assert len(res2.upcoming) == 2  # still listed as upcoming
    # ...but the report still shows everything for meetings in the window, now marked not-new.
    data2 = build_data(res2, ["DART"])
    assert {m["id"] for m in data2["meetings"]} == {m["id"] for m in data["meetings"]}
    assert not any(x["new"] for m in data2["meetings"] for x in m["matches"])

    # Amended agenda re-posted: the old items aren't re-reported, the new one is.
    amended = make_pdf([AGENDA_P1, AGENDA_P2 + "\nItem 15. DART Silver Line update"])
    res3 = run(cfg, http=FakeHttp(routes(amended)), today=TODAY)
    new = [(r["term"], r["hit"]) for r in res3.new_matches]
    assert new and {t for t, _ in new} == {"DART", "Silver Line"}   # only the new Item 15


def test_past_meetings_are_not_redownloaded(tmp_path):
    cfg = config(tmp_path)
    cfg["sources"] = cfg["sources"][:1]
    pdf = make_pdf([AGENDA_P2])
    run(cfg, http=FakeHttp(routes(pdf)), today=TODAY)
    later = FakeHttp(routes(pdf))
    run(cfg, http=later, today=date(2026, 11, 20))
    assert "https://d/agenda1.pdf" not in later.calls


def test_site_files_and_feeds(tmp_path):
    cfg = config(tmp_path)
    res = run(cfg, http=FakeHttp(routes(make_pdf([AGENDA_P1, AGENDA_P2]))), today=TODAY)
    out = write_report(res, ["DART"], tmp_path / "site", site_url="https://agendas.example.org/")
    site = out.parent
    for name in ("index.html", "latest.html", "data.json", "feed.xml", "upcoming.ics", "agenda-report-2026-10-31.html"):
        assert (site / name).exists(), name
    data = json.loads((site / "data.json").read_text())
    assert data["site_url"] == "https://agendas.example.org"

    ns = {"a": "http://www.w3.org/2005/Atom"}
    feed = ET.fromstring(render_atom(data))
    entries = feed.findall("a:entry", ns)
    assert len(entries) == 2  # Dallas committee + Plano council, each first seen today
    assert all(e.find("a:link", ns).get("href").startswith("https://agendas.example.org/#m-") for e in entries)
    assert len({e.find("a:id", ns).text for e in entries}) == 2

    ics = render_ics(data)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert ics.count("BEGIN:VEVENT") == 2 and "DTSTART;VALUE=DATE:20261104" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    assert "SUMMARY:Dallas: Transportation and Infrastructure Committee — " in ics
