"""Google Calendar push: event shape, calendar mapping, idempotent sync, retries and failure handling."""

import copy
import json
import logging
import re
from urllib.parse import unquote, urlparse

import pytest

from agendawatch import gcal
from agendawatch.gcal import GcalError, desired_events, event_body, session_from_env, sync, sync_from_config
from agendawatch.job import EXIT_OK, run_job, sync_google_calendar
from agendawatch.storage import LocalStorage

from helpers import FakeHttp, make_pdf
from test_feeds import build_site
from test_pipeline import AGENDA_P1, AGENDA_P2, TODAY, config, routes

PLANO, DART, ALL = "plano@group.calendar.google.com", "dart@group.calendar.google.com", "all@group.calendar.google.com"
CALENDARS = {"Plano": PLANO, "DART member cities": DART, "*": ALL}


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    @property
    def text(self):
        return json.dumps(self._body) if self._body is not None else ""


class FakeCalendarAPI:
    """Just enough of the Calendar v3 events API: list (private property, timeMin, showDeleted),
    insert (409 on a duplicate id), update, delete (kept as cancelled, like Google)."""

    def __init__(self):
        self.events: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: list[tuple[str, int, dict | None]] = []   # (method, status, body) returned once, in order
        self.broken: set[str] = set()                          # calendars that answer 404

    def request(self, method, url, timeout=None, params=None, json=None):
        m = re.match(r".*/calendars/([^/]+)/events(?:/([^/?]+))?$", urlparse(url).path)
        cal, eid = unquote(m.group(1)), m.group(2)
        self.calls.append((method, cal))
        if self.fail and self.fail[0][0] == method:
            _, status, body = self.fail.pop(0)
            return Resp(status, body)
        if cal in self.broken:
            return Resp(404, {"error": {"message": "Not Found"}})
        store = self.events.setdefault(cal, {})
        if method == "GET":
            prop_key, prop_val = params["privateExtendedProperty"].split("=")
            items = [e for e in store.values()
                     if (e.get("extendedProperties") or {}).get("private", {}).get(prop_key) == prop_val
                     and e["end"]["date"] > params["timeMin"][:10]
                     and (params.get("showDeleted") == "true" or e.get("status") != "cancelled")]
            return Resp(200, {"items": copy.deepcopy(items)})
        if method == "POST":
            if json["id"] in store:
                return Resp(409, {"error": {"message": "The requested identifier already exists."}})
            store[json["id"]] = {**copy.deepcopy(json), "status": "confirmed"}
            return Resp(200, store[json["id"]])
        if method == "PUT":
            store[eid] = copy.deepcopy(json)
            return Resp(200, store[eid])
        if method == "DELETE":
            if eid not in store or store[eid].get("status") == "cancelled":
                return Resp(410, {"error": {"message": "Resource has been deleted"}})
            store[eid]["status"] = "cancelled"
            return Resp(204)
        raise AssertionError(method)

    def live(self, cal):
        return {k: v for k, v in self.events.get(cal, {}).items() if v.get("status") != "cancelled"}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def data(tmp_path):
    return build_site(tmp_path)[1]


def by_city(data, city):
    return next(m for m in data["meetings"] if m["city"] == city)


def no_sleep(_):
    pass


def test_event_body(data):
    m = by_city(data, "Dallas")
    body = event_body(m, data["site_url"])
    assert re.fullmatch(r"[a-v0-9]{5,1024}", body["id"]) and body["id"] == "ag" + m["id"]
    assert body["start"] == {"date": "2026-11-04"} and body["end"] == {"date": "2026-11-05"}
    assert body["summary"].startswith("Dallas: Transportation and Infrastructure Committee")
    assert body["source"]["url"] == f"{data['site_url']}/#m-{m['id']}"
    assert body["extendedProperties"]["private"]["agendawatch"] == "1"
    assert event_body(m, data["site_url"]) == body      # stable across runs


def test_calendar_mapping_by_city_section_and_star(data):
    want = desired_events(data, CALENDARS)
    ids = {cal: {e["summary"].split(":")[0] for e in evs.values()} for cal, evs in want.items()}
    assert ids == {PLANO: {"Plano"}, DART: {"Dallas"}, ALL: {"Dallas", "Plano"}}


def test_sync_is_idempotent_and_tracks_changes(data):
    api = FakeCalendarAPI()
    first = sync(api, data, CALENDARS, sleep=no_sleep)
    assert (first.inserted, first.updated, first.deleted, first.errors) == (4, 0, 0, [])
    assert set(api.live(ALL)) == {"ag" + m["id"] for m in data["meetings"]}

    api.calls.clear()
    again = sync(api, data, CALENDARS, sleep=no_sleep)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 4) and api.writes() == []

    # A new mention in Plano's agenda changes the event text: updated in Plano's and the "*" calendar.
    changed = copy.deepcopy(data)
    plano = by_city(changed, "Plano")
    plano["terms"] = sorted(plano["terms"] + ["Streetcar"])
    res = sync(api, changed, CALENDARS, sleep=no_sleep)
    assert (res.updated, res.unchanged) == (2, 2)
    assert "Streetcar" in api.live(PLANO)["ag" + plano["id"]]["summary"]

    # Plano's meeting is no longer listed (e.g. the body was renamed): removed from both calendars.
    changed["meetings"] = [m for m in changed["meetings"] if m["city"] != "Plano"]
    res = sync(api, changed, CALENDARS, sleep=no_sleep)
    assert res.deleted == 2 and not api.live(PLANO) and len(api.live(ALL)) == 1

    # ...and comes back: the cancelled event is restored, not duplicated.
    res = sync(api, data, CALENDARS, sleep=no_sleep)
    assert res.updated == 2 and res.inserted == 0 and len(api.live(ALL)) == 2


def test_events_before_the_window_and_foreign_events_are_left_alone(data):
    api = FakeCalendarAPI()
    old = {"id": "agold", "summary": "old meeting", "status": "confirmed",
           "start": {"date": "2026-09-01"}, "end": {"date": "2026-09-02"},
           "extendedProperties": {"private": {"agendawatch": "1", "hash": "x"}}}
    mine_in_window = {**old, "id": "agstale", "summary": "stale", "start": {"date": data["window"]["start"]},
                      "end": {"date": "2099-01-01"}}
    someone_elses = {"id": "lunch", "summary": "Lunch", "status": "confirmed",
                     "start": {"date": "2026-11-04"}, "end": {"date": "2026-11-05"}}
    api.events[ALL] = {e["id"]: copy.deepcopy(e) for e in (old, mine_in_window, someone_elses)}
    res = sync(api, data, {"*": ALL}, sleep=no_sleep)
    live = api.live(ALL)
    assert res.deleted == 1 and "agstale" not in live
    assert "agold" in live and "lunch" in live


def test_retries_rate_limits(data):
    api = FakeCalendarAPI()
    api.fail = [("GET", 429, None),
                ("GET", 403, {"error": {"errors": [{"reason": "rateLimitExceeded"}], "message": "slow down"}})]
    slept = []
    res = sync(api, data, {"Plano": PLANO}, sleep=slept.append)
    assert res.errors == [] and res.inserted == 1 and slept == [1, 2]


def test_dry_run_changes_nothing(data):
    api = FakeCalendarAPI()
    res = sync(api, data, CALENDARS, dry_run=True, sleep=no_sleep)
    assert res.inserted == 4 and api.writes() == []
    assert len(res.actions) == 4 and all(a.startswith("add") for a in res.actions)


def test_one_broken_calendar_does_not_stop_the_others(data):
    api = FakeCalendarAPI()
    api.broken.add(PLANO)
    res = sync(api, data, CALENDARS, sleep=no_sleep)
    assert len(res.errors) == 1 and PLANO in res.errors[0] and "shared with the service account" in res.errors[0]
    assert len(api.live(ALL)) == 2 and len(api.live(DART)) == 1


def test_skipped_without_calendars_or_credentials(data):
    assert sync_from_config({}, data, env={}) is None
    assert sync_from_config({"google_calendar": {"calendars": {"*": ALL}}}, data, env={}) is None


def test_bad_credentials_are_a_clear_error():
    with pytest.raises(GcalError, match="service account key"):
        session_from_env({"AGENDAWATCH_GOOGLE_CREDENTIALS": '{"type": "service_account"}'})


def test_job_pushes_after_publishing_and_survives_failures(tmp_path, caplog):
    cfg = config(tmp_path)
    cfg["google_calendar"] = {"calendars": {"*": ALL}}
    pdf = make_pdf([AGENDA_P1, AGENDA_P2])
    api = FakeCalendarAPI()
    storage = LocalStorage(tmp_path / "hosted")
    with caplog.at_level(logging.INFO):
        assert run_job(json.loads(json.dumps(cfg)), storage, today=TODAY, http=FakeHttp(routes(pdf)),
                       gcal_session=api) == EXIT_OK
    assert len(api.live(ALL)) == 2
    assert any(getattr(r, "fields", {}).get("event") == "gcal_synced" for r in caplog.records)

    # Google is down: the run still succeeds, with a gcal_failed warning.
    class Down:
        def request(self, *a, **kw):
            raise ConnectionError("network unreachable")

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert run_job(json.loads(json.dumps(cfg)), storage, today=TODAY, http=FakeHttp(routes(pdf)),
                       gcal_session=Down()) == EXIT_OK
    assert any(getattr(r, "fields", {}).get("event") == "gcal_failed" for r in caplog.records)


def test_sync_google_calendar_never_raises(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        sync_google_calendar({"google_calendar": {"calendars": {"*": ALL}}}, tmp_path / "missing.json",
                             session=FakeCalendarAPI())
    assert any(getattr(r, "fields", {}).get("event") == "gcal_failed" for r in caplog.records)


def test_module_documents_its_config():
    assert "google_calendar:" in gcal.__doc__
