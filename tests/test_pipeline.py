"""End-to-end: list → download → extract → match → dedupe → report."""

from datetime import date
from pathlib import Path

import yaml

from agendawatch.http import FetchError
from agendawatch.report import build_html, text_summary
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
    assert terms == {"DART", "Dallas Area Rapid Transit", "Transit", "General Mobility Program", "GMP", "Streetcar"}
    assert all(r["page"] == 2 for r in res.new_matches if r["url"] == "https://d/agenda1.pdf")
    # GMP in the Park Board construction item is filtered out as "Guaranteed Maximum Price"
    assert not [r for r in res.new_matches if r["url"] == "https://d/agenda2.pdf"]
    by_status = {s.name: s for s in res.statuses}
    assert by_status["Plano"].new_matches == 1
    assert "503" in by_status["Addison"].error  # one broken site doesn't stop the run
    assert len(res.upcoming) == 2

    html = build_html(res, ["DART"])
    assert "<mark>DART</mark>" in html and "#page=2" in html and "Transportation and Infrastructure" in html
    assert "couldn't be read today" in html
    summary = text_summary(res)
    assert "new mention" in summary
    assert summary.index("DART member cities") < summary.index("Dallas —") < summary.index("DCTA member cities") < summary.index("Plano —")

    # Each section gets its own block, in config order, holding its own meetings.
    dart_at, dcta_at = html.index(">DART member cities</h2>"), html.index(">DCTA member cities</h2>")
    assert dart_at < html.index("Transportation and Infrastructure") < dcta_at < html.index("Plano · City Council")
    assert "Other sources" not in html.split(">Sources</h2>")[0]

    # Second run, nothing changed: no new matches, nothing re-extracted.
    res2 = run(cfg, http=FakeHttp(routes(pdf)), today=TODAY)
    assert res2.new_matches == []
    assert {s.name: s.unchanged for s in res2.statuses}["Dallas"] == 2
    assert len(res2.upcoming) == 2  # still listed as upcoming

    # Amended agenda re-posted: the old items aren't re-reported, the new one is.
    amended = make_pdf([AGENDA_P1, AGENDA_P2 + "\nItem 15. DART Silver Line update"])
    res3 = run(cfg, http=FakeHttp(routes(amended)), today=TODAY)
    new = [(r["term"], r["hit"]) for r in res3.new_matches]
    assert new and all(t == "DART" for t, _ in new)


def test_past_meetings_are_not_redownloaded(tmp_path):
    cfg = config(tmp_path)
    cfg["sources"] = cfg["sources"][:1]
    pdf = make_pdf([AGENDA_P2])
    run(cfg, http=FakeHttp(routes(pdf)), today=TODAY)
    later = FakeHttp(routes(pdf))
    run(cfg, http=later, today=date(2026, 11, 20))
    assert "https://d/agenda1.pdf" not in later.calls
