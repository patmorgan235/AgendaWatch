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


def paths(current):
    return {f["name"]: f["path"] for f in current}


def test_feed_paths_do_not_depend_on_config_order():
    from agendawatch.feeds import assign

    names = [("city", "St. Paul"), ("city", "St Paul"), ("city", "Dallas"), ("city", "東京"), ("city", "大阪")]
    a, _ = assign(names, [])
    b, _ = assign(list(reversed(names)), [])
    assert paths(a) == paths(b)
    assert paths(a)["Dallas"] == "feeds/city/dallas"
    # colliding slugs (and names with no ASCII letters, which all slug to "feed") get a hash suffix each
    assert len(set(paths(a).values())) == 5
    assert re.fullmatch(r"feeds/city/st-paul-[0-9a-f]{6}", paths(a)["St Paul"])
    assert re.fullmatch(r"feeds/city/feed-[0-9a-f]{6}", paths(a)["東京"])


def test_feed_paths_are_kept_and_never_reused():
    from agendawatch.feeds import assign

    history = [{"kind": "city", "name": "St Paul", "path": "feeds/city/st-paul", "last_published": "2026-10-01T06:00:00"}]
    # A new look-alike name can't take an address that's already in use...
    current, retired = assign([("city", "St Paul"), ("city", "St. Paul")], history)
    assert paths(current)["St Paul"] == "feeds/city/st-paul"
    assert paths(current)["St. Paul"].startswith("feeds/city/st-paul-") and retired == []
    # ...nor one that was used before, even after its city left the config; that one is retired.
    current, retired = assign([("city", "St. Paul")], history)
    assert paths(current)["St. Paul"] != "feeds/city/st-paul"
    assert [r["path"] for r in retired] == ["feeds/city/st-paul"]


def test_nested_feeds_link_back_to_the_page_without_site_url(tmp_path):
    import xml.etree.ElementTree as ET

    ns = {"a": "http://www.w3.org/2005/Atom"}
    site, _ = build_site(tmp_path, site_url=None)
    dallas = ET.fromstring((site / "feeds/city/dallas.xml").read_text(encoding="utf-8"))
    assert dallas.find("a:link[@rel='alternate']", ns).get("href") == "../../index.html?city=Dallas"
    entry_links = [e.find("a:link", ns).get("href") for e in dallas.findall("a:entry", ns)]
    assert entry_links and all(h.startswith("../../index.html#m-") for h in entry_links)
    root = ET.fromstring((site / "feed.xml").read_text(encoding="utf-8"))
    assert root.find("a:link[@rel='alternate']", ns).get("href") == "index.html"


def test_removed_source_keeps_feeds_while_on_the_page_then_they_are_retired(tmp_path):
    from datetime import timedelta

    cfg = config(tmp_path)
    pdf = make_pdf([AGENDA_P1, AGENDA_P2])

    def run_and_write(cfg, today):
        res = run(cfg, http=FakeHttp(routes(pdf)), today=today)
        index = write_report(res, ["DART"], tmp_path / "site", site_url=SITE)
        return index.parent, json.loads((index.parent / "data.json").read_text(encoding="utf-8"))

    run_and_write(cfg, TODAY)
    # Plano's source is removed from the config; its Nov 9 meeting is still in the report window.
    cfg["sources"] = [s for s in cfg["sources"] if s["name"] != "Plano"]
    site, data = run_and_write(cfg, TODAY)
    feeds = {(f["kind"], f["name"]): f for f in data["feeds"]}
    assert feeds[("city", "Plano")]["ics"] == "feeds/city/plano.ics"              # same address as before
    assert ("section", "Other sources") in feeds                                  # where the page now lists it
    for path in ("feeds/city/plano.ics", "feeds/section/other-sources.ics"):
        assert "SUMMARY:Plano:" in (site / path).read_text(encoding="utf-8")
    # Its old section has no sources or meetings left: the feed is emptied, with a notice.
    dcta = (site / "feeds/section/dcta-member-cities.ics").read_bytes().decode("utf-8")
    assert "BEGIN:VEVENT" not in dcta and "no longer updated" in dcta and "X-WR-CALDESC:" in dcta
    assert "no longer updated" in (site / "feeds/section/dcta-member-cities.xml").read_text(encoding="utf-8")
    assert ("section", "DCTA member cities") not in feeds                         # and it's not offered on the page

    # Weeks later the meeting has left the window: Plano's city feed is retired too.
    site, data = run_and_write(cfg, TODAY + timedelta(days=45))
    assert ("city", "Plano") not in {(f["kind"], f["name"]) for f in data["feeds"]}
    plano = (site / "feeds/city/plano.ics").read_bytes().decode("utf-8")
    assert "BEGIN:VEVENT" not in plano and "Plano is no longer watched" in plano.replace("\r\n ", "")


def test_page_has_calendar_view(tmp_path):
    site, data = build_site(tmp_path)
    html = (site / "index.html").read_text(encoding="utf-8")
    for needle in ('id="v-list"', 'id="v-cal"', "function renderCalendar", 'id: "day-panel"', "Add to calendar",
                   'id="subscribe-wrap"', "function renderFeeds"):
        assert needle in html, needle
    embedded = html.split('<script id="data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(embedded)["meetings"][0]["add"] == data["meetings"][0]["add"]
