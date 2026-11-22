"""Source health: run history, failure streaks, lead time (notice) and stale sources."""

from datetime import date, datetime, timedelta

from agendawatch.http import FetchError
from agendawatch.runner import SourceStatus, run
from agendawatch.site import build_data
from agendawatch.sources import Document
from agendawatch.store import Store

from helpers import FakeHttp, make_pdf


def clock(day: date):
    return lambda: datetime.combine(day, datetime.min.time()).isoformat(timespec="seconds")


def doc(url, meeting, body="City Council"):
    return Document("Src", "City", body, meeting, "agenda", url)


def test_lead_time_per_meeting_skips_backfill(tmp_path):
    db = tmp_path / "h.db"
    s = Store(db, now=clock(date(2026, 10, 1)))           # first day = backfill
    s.save_document(doc("u0", date(2026, 9, 28)), "a", ["x"], "ok")
    s.now = clock(date(2026, 10, 10))
    s.save_document(doc("u1", date(2026, 10, 16)), "a", ["x"], "ok")          # 6 days ahead
    s.now = clock(date(2026, 10, 13))
    s.save_document(doc("u1-packet", date(2026, 10, 16)), "a", ["x"], "ok")   # same meeting, later: ignored
    s.now = clock(date(2026, 10, 20))
    s.save_document(doc("u2", date(2026, 10, 30)), "a", ["x"], "ok")          # 10 days ahead
    s.now = clock(date(2026, 11, 3))
    s.save_document(doc("u3", date(2026, 11, 2)), "a", ["x"], "ok")           # posted a day after (−1)
    h = s.source_health(date(2026, 11, 5))["Src"]
    assert h["first_ever"] == "2026-10-01" and h["last_new"] == "2026-11-03"
    assert h["lead_samples"] == 3 and h["lead_days"] == 6
    s.close()


def test_failure_streak_counts_consecutive_errors(tmp_path):
    s = Store(tmp_path / "f.db")
    for i, err in enumerate(["", "HTTP 403", "HTTP 403", "HTTP 403"], start=1):
        rid = s.start_run()
        s.record_source_run(rid, SourceStatus("Hurst", "Hurst", "web page", error=err))
        s.record_source_run(rid, SourceStatus("Plano", "Plano", "Legistar"))
    h = s.source_health(date.today())
    assert h["Hurst"]["failing_runs"] == 3 and h["Hurst"]["failing_since"]
    assert "Plano" not in h or h["Plano"]["failing_runs"] == 0
    rid = s.start_run()
    s.record_source_run(rid, SourceStatus("Hurst", "Hurst", "web page"))   # recovered
    assert s.source_health(date.today()).get("Hurst", {}).get("failing_runs", 0) == 0
    s.close()


# ---------------------------------------------------------------- end to end, over simulated days
PAGE = "https://city.example/agendas"


def page(*items):
    return "<table>" + "".join(
        f'<tr><td>{d:%B %d, %Y} City Council</td><td><a href="/a/{n}.pdf">Agenda</a></td></tr>' for n, d in items
    ) + "</table>"


def cfg(tmp_path, **settings):
    return {
        "settings": {"database": str(tmp_path / "e.db"), "report_dir": str(tmp_path / "r"),
                     "request_delay_seconds": 0, "lookback_days": 10, "lookahead_days": 60, **settings},
        "terms": [{"name": "DART", "case_sensitive": True}],
        "sources": [{"name": "City", "type": "link_page", "urls": [PAGE], "link_pattern": r"/a/\d+\.pdf",
                     "section": "DART member cities"}],
    }


def routes(*items):
    r = {PAGE: page(*items)}
    for n, _ in items:
        r[f"https://city.example/a/{n}.pdf"] = make_pdf([f"Agenda {n}. Item: DART bus stop."])
    return r


def status(res):
    return res.statuses[0]


def test_lead_time_and_stale_over_simulated_days(tmp_path):
    c = cfg(tmp_path)
    d0 = date(2026, 9, 1)
    m1 = (1, d0 + timedelta(days=2))
    # day 0: first run (backfill) — no lead time yet, not stale (source is new)
    st = status(run(c, http=FakeHttp(routes(m1)), today=d0))
    assert st.last_new == d0.isoformat() and st.lead_samples == 0 and not st.stale

    # three later meetings, each posted 7 days ahead
    items = [m1]
    for k in range(1, 4):
        day = d0 + timedelta(days=7 * k)
        items.append((k + 1, day + timedelta(days=7)))
        st = status(run(c, http=FakeHttp(routes(*items)), today=day))
    assert st.lead_samples == 3 and st.lead_days == 7 and not st.stale

    # the city goes quiet (moved platforms?): 40 days with nothing new → stale
    quiet = d0 + timedelta(days=21 + 40)
    res = run(c, http=FakeHttp(routes()), today=quiet)
    st = status(res)
    assert st.stale and st.stale_after_days == 30 and st.last_new == (d0 + timedelta(days=21)).isoformat()
    data = build_data(res, ["DART"])
    assert data["sources"][0]["stale"] and data["sources"][0]["lead_days"] == 7

    # a per-source override relaxes the check
    c2 = cfg(tmp_path)
    c2["sources"][0]["stale_after_days"] = 90
    assert not status(run(c2, http=FakeHttp(routes()), today=quiet)).stale

    # a source that errors is reported as failing, not stale
    st = status(run(c, http=FakeHttp({PAGE: FetchError("HTTP 403")}), today=quiet + timedelta(days=1)))
    assert st.error and not st.stale and st.failing_runs == 1
