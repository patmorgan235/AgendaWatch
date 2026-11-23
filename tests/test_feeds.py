"""Calendar output: per-meeting .ics files, "Add to Google Calendar" links, and the page's calendar view."""

import json
import re
from urllib.parse import parse_qs, urlparse

from agendawatch.report import write_report
from agendawatch.runner import run

from helpers import FakeHttp, make_pdf
from test_pipeline import AGENDA_P1, AGENDA_P2, TODAY, config, routes

SITE = "https://agendas.example.org"


def build_site(tmp_path, site_url=SITE):
    cfg = config(tmp_path)
    res = run(cfg, http=FakeHttp(routes(make_pdf([AGENDA_P1, AGENDA_P2]))), today=TODAY)
    index = write_report(res, ["DART"], tmp_path / "site", site_url=site_url)
    return index.parent, json.loads((index.parent / "data.json").read_text(encoding="utf-8"))


def uids(ics: str) -> set[str]:
    return set(re.findall(r"^UID:([^\r\n]+)", ics, re.M))


def test_each_dated_meeting_gets_its_own_ics(tmp_path):
    site, data = build_site(tmp_path)
    dated = [m for m in data["meetings"] if m["date"]]
    assert dated
    upcoming = (site / "upcoming.ics").read_text(encoding="utf-8")
    for m in dated:
        assert m["add"]["ics"] == f"feeds/meeting/{m['id']}.ics"
        raw = (site / m["add"]["ics"]).read_bytes()
        ics = raw.decode("utf-8")
        assert raw.count(b"\r\n") == ics.count("\n")          # CRLF line endings throughout
        assert ics.count("BEGIN:VEVENT") == 1
        # same UID as in the subscribed feed, so importing both doesn't duplicate the event
        assert uids(ics) == {f"{m['id']}@agendawatch"} <= uids(upcoming)
        assert f"DTSTART;VALUE=DATE:{m['date'].replace('-', '')}" in ics
        assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


def test_google_add_link(tmp_path):
    _, data = build_site(tmp_path)
    m = next(m for m in data["meetings"] if m["city"] == "Dallas")
    url = urlparse(m["add"]["google"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "calendar.google.com" and q["action"] == "TEMPLATE"
    assert q["dates"] == "20261104/20261105"                  # all-day: end date is exclusive
    assert q["text"].startswith("Dallas: Transportation and Infrastructure Committee — ")
    assert q["details"].startswith(f"{SITE}/#m-{m['id']}")
    assert "DART" in q["details"] and len(q["details"]) <= 1500


def test_google_add_link_without_site_url(tmp_path):
    _, data = build_site(tmp_path, site_url=None)
    m = next(m for m in data["meetings"] if m["city"] == "Dallas")
    q = parse_qs(urlparse(m["add"]["google"]).query)
    assert q["details"][0].startswith("Agenda mentions:")   # no site link to lead with


def test_page_has_calendar_view(tmp_path):
    site, data = build_site(tmp_path)
    html = (site / "index.html").read_text(encoding="utf-8")
    for needle in ('id="v-list"', 'id="v-cal"', "function renderCalendar", 'id: "day-panel"', "Add to calendar"):
        assert needle in html, needle
    embedded = html.split('<script id="data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(embedded)["meetings"][0]["add"] == data["meetings"][0]["add"]
