import yaml
from pathlib import Path

from agendawatch.dates import parse_date
from agendawatch.matcher import compile_terms, find_hits

CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
TERMS = compile_terms(CONFIG["terms"])


def names(text):
    hits, counts = find_hits([text], TERMS)
    return counts


def test_dart_is_case_sensitive_whole_word():
    assert names("Interlocal agreement with DART for bus service") == {"DART": 1}
    assert "DART" not in names("Purchase from Dart Container Corporation")
    assert "DART" not in names("DARTboard installation")


def test_dcta_terms():
    counts = names("Interlocal agreement with the Denton County\nTransportation Authority (DCTA) for GoZone service")
    assert counts["DCTA"] == 1 and counts["Denton County Transportation Authority"] == 1
    assert "DCTA" not in names("dcta.net")  # acronym is case-sensitive


def test_full_name_across_line_break():
    c = names("agreement with the Dallas Area\nRapid   Transit authority")
    assert c["Dallas Area Rapid Transit"] == 1
    assert c["Transit"] == 1


def test_transit_whole_word():
    assert names("transit-oriented development near the station") == {"Transit": 1}
    assert "Transit" not in names("transitional housing program")


def test_gmp_skips_guaranteed_maximum_price():
    assert "GMP" not in names("Approve a GMP (Guaranteed Maximum Price) amendment with the CMAR")
    assert names("DART GMP funds for the Alpha Road project") == {"DART": 1, "GMP": 1}
    assert "GMP" not in names("gmp lowercase is not the acronym")


def test_general_mobility_program():
    assert names("Use of General Mobility Program funds")["General Mobility Program"] == 1


def test_streetcar_variants():
    for text in ("Dallas Streetcar extension", "street car service", "street-car", "the streetcars run"):
        assert names(text).get("Streetcar") == 1, text


def test_snippets_capture_context_and_page():
    pages = ["nothing here", "Item 12. Authorize payment to DART for the GMP program."]
    hits, _ = find_hits(pages, TERMS, context=20)
    dart = [h for h in hits if h.term == "DART"][0]
    assert dart.page == 2 and dart.hit == "DART"
    assert "payment to" in dart.before


def test_max_snippets_per_term():
    hits, counts = find_hits(["DART " * 20 + "x " * 400 + "DART"], TERMS, max_per_term=3)
    assert counts["DART"] == 21
    assert len([h for h in hits if h.term == "DART"]) <= 3


def test_parse_dates():
    from datetime import date
    assert parse_date("City Council 10/27/2026") == date(2026, 10, 27)
    assert parse_date("11/05/26 City Council Meeting") == date(2026, 11, 5)
    assert parse_date("City Council Oct 26, 2026 02h 42m") == date(2026, 10, 26)
    assert parse_date("2026-11-04T00:00:00") == date(2026, 11, 4)
    assert parse_date("/docs/2026/CM/20260414_7575/AGENDApacket.pdf") == date(2026, 4, 14)
    assert parse_date("August 3-4, 2026") == date(2026, 8, 3)
    assert parse_date("Aug 3 & 4, 2026") == date(2026, 8, 3)
    assert parse_date("agendapacketrtc11052026.pdf") == date(2026, 11, 5)
    assert parse_date("agendartc1-23-2025.pdf") == date(2025, 1, 23)
    assert parse_date("agendartc03122026ts.pdf") == date(2026, 3, 12)
    assert parse_date("/docs/2026/CM/20260414_7575/x.pdf") == date(2026, 4, 14)  # YYYYMMDD still wins
    assert parse_date("February25, 2020") == date(2020, 2, 25)
    assert parse_date("no date") is None
