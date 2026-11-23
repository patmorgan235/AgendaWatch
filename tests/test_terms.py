"""Tagged terms, overlap rules, noise rules, action tags, and re-matching after the term list changes."""

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from agendawatch.matcher import compile_terms, find_hits
from agendawatch.runner import run
from agendawatch.site import build_data, taxonomy_from_config

from helpers import FakeHttp, make_pdf

CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
TERMS = compile_terms(CONFIG["terms"])


def counts(text):
    return find_hits([text], TERMS)[1]


@pytest.mark.parametrize("text, expected", [
    ("DART Silver Line and the Cotton Belt corridor", {"DART": 1, "Silver Line": 2}),
    ("Trinity Railway Express (TRE) and TEXRail service", {"TRE": 2, "TEXRail": 1}),
    ("Sunday TRE Plus Incremental Fuel Charge", {"TRE": 1}),
    ("light rail and the D2 subway project", {"Light rail": 1, "D2 subway": 1}),
    ("high-speed rail station", {"High-speed rail": 1}),
    ("AMTRAK Heartland Flyer and the Texas Eagle", {"Amtrak": 3}),
    ("regional passenger rail and passenger trains", {"Passenger rail": 2}),
    ("M-Line trolley, McKinney Avenue Transit Authority", {"M-Line trolley": 2}),
    ("bus rapid transit (BRT) on Lancaster", {"Bus rapid transit": 2}),
    ("new bus shelters and bus stops", {"Bus stop or shelter": 2}),
    ("a park-and-ride lot", {"Park-and-ride": 1}),
    ("GoLink zone and microtransit pilot", {"GoLink": 1, "Microtransit": 1}),
    ("Arlington On-Demand rideshare", {"On-demand transit": 1}),
    ("STAR Transit briefing", {"STAR Transit": 1}),
    ("contract with SPAN, Inc.", {"SPAN": 1}),
    ("Collin County Transit service", {"Collin County Transit": 1}),
    ("withdrawal election from DART", {"DART membership": 1, "DART": 1}),
    ("Complete Streets policy and the bike/ped plan", {"Complete Streets": 1, "Bicycle and pedestrian": 1}),
    ("transit-oriented development (TOD) overlay", {"Transit-oriented development": 2}),
    ("regional transit plan", {"Transit": 1}),
])
def test_new_terms(text, expected):
    assert counts(text) == expected


@pytest.mark.parametrize("text", [
    "Purchase of a Ford Transit Connect van for Parks",
    "insurance for goods in transit",
    "replace the bridge span",          # bare "span"
    "the brt program",                  # lower-case acronym
    "tod is a word",                    # lower-case acronym
    "(8) Homeless TRE/TRE Thomas Robert Edward Ballot Incentive",   # initials, not the railway
])
def test_noise_is_not_matched(text):
    assert counts(text) == {}


def test_longest_match_wins_across_terms():
    c = counts("Interlocal agreement with Dallas Area Rapid Transit and STAR Transit")
    assert c == {"DART": 1, "STAR Transit": 1}   # no bare "Transit"


def test_tags_must_be_group_value():
    with pytest.raises(ValueError, match="group:value"):
        compile_terms([{"name": "X", "tags": ["rail"]}])


# ---------------------------------------------------------------- report: tags and action tags
PAGE = "https://city.example/agendas"


def cfg(tmp_path, terms):
    return {
        "settings": {"database": str(tmp_path / "t.db"), "report_dir": str(tmp_path / "r"),
                     "request_delay_seconds": 0, "lookback_days": 10, "lookahead_days": 60},
        "terms": terms,
        "actions": CONFIG["actions"],
        "sources": [{"name": "City", "type": "link_page", "urls": [PAGE], "link_pattern": r"/a/\d+\.pdf",
                     "section": "DART member cities"}],
    }


def routes(items):
    r = {PAGE: "<table>" + "".join(
        f'<tr><td>{d:%B %d, %Y} City Council</td><td><a href="/a/{n}.pdf">Agenda</a></td></tr>' for n, d, _ in items
    ) + "</table>"}
    for n, _, text in items:
        r[f"https://city.example/a/{n}.pdf"] = make_pdf(text.split("\n"))
    return r


def test_mentions_carry_tags_and_actions(tmp_path):
    c = cfg(tmp_path, CONFIG["terms"])
    day = date(2026, 9, 1)
    res = run(c, http=FakeHttp(routes([(1, day + timedelta(days=5),
                                        "Item 4. Approve an interlocal agreement with DART for the Silver Line.\n"
                                        "Item 9. Transit mentioned in the city manager's list of agencies.")])), today=day)
    data = build_data(res, ["DART"], taxonomy=taxonomy_from_config(c))
    [m] = data["meetings"]
    hits = {x["term"]: x for x in m["matches"]}
    assert hits["Silver Line"]["tags"] == ["agency:DART", "mode:rail"]
    assert "agreement" in hits["DART"]["actions"]
    assert hits["Transit"]["actions"] == [] and hits["Transit"]["tags"] == []
    assert {"agency:DART", "mode:rail"} <= set(m["tags"]) and "agreement" in m["actions"]
    assert "DART" in data["taxonomy"]["groups"]["agency"] and "rail" in data["taxonomy"]["groups"]["mode"]
    assert "public hearing" in data["taxonomy"]["actions"]
    # Tags are applied when the report is built: changing them re-tags without re-reading anything.
    retagged = taxonomy_from_config({**c, "terms": [{**t, "tags": ["topic:test"]} if isinstance(t, dict) and t["name"] == "Silver Line"
                                                    else t for t in c["terms"]]})
    data2 = build_data(res, ["DART"], taxonomy=retagged)
    assert {x["term"]: x for x in data2["meetings"][0]["matches"]}["Silver Line"]["tags"] == ["topic:test"]


# ---------------------------------------------------------------- re-matching stored agendas
def test_changing_terms_rematches_stored_agendas(tmp_path):
    old_terms = [{"name": "Transit"}]
    c = cfg(tmp_path, old_terms)
    day = date(2026, 9, 1)
    past, upcoming = day - timedelta(days=3), day + timedelta(days=6)
    items = [(1, past, "Item 2. Purchase a Ford Transit van.\nItem 3. Transit-oriented development overlay."),
             (2, upcoming, "Item 5. Silver Line station area plan.\nItem 6. Regional transit plan.")]
    first = run(c, http=FakeHttp(routes(items)), today=day)
    assert {m["term"] for m in first.new_matches} == {"Transit"}

    # New terms and noise rules arrive a week later; nothing is re-downloaded (the past agenda is skipped).
    c2 = {**c, "terms": CONFIG["terms"]}
    later = day + timedelta(days=1)
    res = run(c2, http=FakeHttp(routes(items)), today=later)
    window = {(r["meeting_date"], r["term"]): r for r in res.window_matches}
    assert (past.isoformat(), "Transit") not in window            # Ford Transit dropped; TOD took the other
    assert (past.isoformat(), "Transit-oriented development") in window
    assert (upcoming.isoformat(), "Silver Line") in window
    new_terms = {(r["meeting_date"], r["term"]) for r in res.new_matches}
    assert (upcoming.isoformat(), "Silver Line") in new_terms                      # upcoming: news
    assert (past.isoformat(), "Transit-oriented development") not in new_terms     # past: back-dated, not "new"

    # Same terms again: nothing re-matched, nothing new.
    again = run(c2, http=FakeHttp(routes(items)), today=later)
    assert again.new_matches == []
