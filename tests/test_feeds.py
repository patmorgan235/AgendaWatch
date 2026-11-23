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


def test_section_and_city_feeds(tmp_path):
    import xml.etree.ElementTree as ET

    site, data = build_site(tmp_path)
    feeds = {(f["kind"], f["name"]): f for f in data["feeds"]}
    # every configured source's city and section, including Addison, whose site failed (no meetings)
    assert set(feeds) == {("section", "DART member cities"), ("section", "DCTA member cities"),
                          ("city", "Dallas"), ("city", "Plano"), ("city", "Addison")}
    assert feeds[("city", "Dallas")]["ics"] == "feeds/city/dallas.ics"
    assert feeds[("section", "DART member cities")]["atom"] == "feeds/section/dart-member-cities.xml"

    ns = {"a": "http://www.w3.org/2005/Atom"}
    for f in data["feeds"]:
        ics = (site / f["ics"]).read_text(encoding="utf-8")
        assert ics.startswith("BEGIN:VCALENDAR") and "REFRESH-INTERVAL;VALUE=DURATION:PT12H" in ics
        assert all(len(line.encode()) <= 75 for line in ics.split("\n"))
        atom = ET.fromstring((site / f["atom"]).read_text(encoding="utf-8"))
        assert atom.find("a:link[@rel='self']", ns).get("href") == f"{SITE}/{f['atom']}"
        assert f["google"].startswith("https://calendar.google.com/calendar/r?cid=webcal%3A%2F%2F")

    addison = (site / feeds[("city", "Addison")]["ics"]).read_bytes().decode("utf-8")
    assert "BEGIN:VEVENT" not in addison and addison.endswith("END:VCALENDAR\r\n")   # empty but valid
    dallas = (site / feeds[("city", "Dallas")]["ics"]).read_text(encoding="utf-8")
    plano = (site / feeds[("city", "Plano")]["ics"]).read_text(encoding="utf-8")
    assert "SUMMARY:Dallas:" in dallas and "Plano" not in dallas.split("BEGIN:VEVENT", 1)[1]
    assert "SUMMARY:Plano:" in plano and "LAST-MODIFIED:" in plano
    dart = ET.fromstring((site / feeds[("section", "DART member cities")]["atom"]).read_text(encoding="utf-8"))
    titles = [e.find("a:title", ns).text for e in dart.findall("a:entry", ns)]
    assert titles and all(t.startswith("Dallas") for t in titles)
    assert dart.find("a:link[@rel='alternate']", ns).get("href") == f"{SITE}/?section=DART+member+cities"
    # feed ids are unique per feed
    ids = {ET.fromstring((site / f["atom"]).read_text(encoding="utf-8")).find("a:id", ns).text for f in data["feeds"]}
    assert len(ids) == len(data["feeds"])


def test_feeds_without_site_url_have_no_google_link(tmp_path):
    _, data = build_site(tmp_path, site_url=None)
    assert data["feeds"] and not any("google" in f for f in data["feeds"])


def test_slug_collisions_get_a_suffix():
    from types import SimpleNamespace

    from agendawatch.site import feed_list

    res = SimpleNamespace(statuses=[SimpleNamespace(section="A", city="St. Paul"),
                                    SimpleNamespace(section="A", city="St Paul")])
    cities = [f["ics"] for f in feed_list(res, None) if f["kind"] == "city"]
    assert cities == ["feeds/city/st-paul.ics", "feeds/city/st-paul-2.ics"]


def test_page_has_calendar_view(tmp_path):
    site, data = build_site(tmp_path)
    html = (site / "index.html").read_text(encoding="utf-8")
    for needle in ('id="v-list"', 'id="v-cal"', "function renderCalendar", 'id: "day-panel"', "Add to calendar",
                   'id="subscribe-wrap"', "function renderFeeds"):
        assert needle in html, needle
    embedded = html.split('<script id="data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(embedded)["meetings"][0]["add"] == data["meetings"][0]["add"]
