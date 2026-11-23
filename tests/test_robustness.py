"""A bad file can't stop a run, and unchanged files aren't downloaded again (ETag / Last-Modified)."""

import sqlite3

import pytest
from requests.structures import CaseInsensitiveDict

from agendawatch import runner
from agendawatch.extract import extract_pages
from agendawatch.http import FetchError, Http
from agendawatch.store import Store

from helpers import FakeHttp, Resp, make_pdf
from test_pipeline import AGENDA_P1, AGENDA_P2, TODAY, config, routes

DALLAS_AGENDA = "https://d/agenda1.pdf"
PARK_AGENDA = "https://d/agenda2.pdf"


def by_name(res):
    return {s.name: s for s in res.statuses}


# ---------------------------------------------------------------- bad files
def test_unreadable_file_does_not_stop_the_run(tmp_path):
    cfg = config(tmp_path)
    r = routes(make_pdf([AGENDA_P1, AGENDA_P2]))
    r[PARK_AGENDA] = b"%PDF-1.4 truncated"   # pypdf raises PdfStreamError on this

    res = run_ok(cfg, r)
    st = by_name(res)
    assert st["Dallas"].failed == 1 and st["Dallas"].read == 1
    assert any("couldn't read the file" in n and "PdfStreamError" in n for n in st["Dallas"].notes)
    assert st["Plano"].new_matches == 1           # sources after the bad file still ran
    assert any(m["url"] == DALLAS_AGENDA for m in res.new_matches)

    # The bad file is tried again next run, from scratch (no conditional request).
    r[PARK_AGENDA] = make_pdf(["Item 3. Park Board report"])
    http = FakeHttp(r)
    st = by_name(runner.run(cfg, http=http, today=TODAY))
    assert PARK_AGENDA in http.calls and PARK_AGENDA not in http.conditional
    assert st["Dallas"].failed == 0 and st["Dallas"].read == 1


def test_unexpected_error_in_one_document_is_contained(tmp_path, monkeypatch):
    real = runner.find_hits

    def find_hits(pages, terms, **kw):
        if any("Guaranteed Maximum Price" in p for p in pages):
            raise RuntimeError("boom")
        return real(pages, terms, **kw)

    monkeypatch.setattr(runner, "find_hits", find_hits)
    res = run_ok(config(tmp_path), routes(make_pdf([AGENDA_P1, AGENDA_P2])))
    st = by_name(res)
    assert st["Dallas"].failed == 1 and any("RuntimeError: boom" in n for n in st["Dallas"].notes)
    assert st["Plano"].new_matches == 1


def test_html_labelled_pdf_is_read_as_html():
    pages, note = extract_pages(b"<!DOCTYPE html><html><body>Downloading, please wait</body></html>",
                                "application/pdf")
    assert note == "HTML page" and "please wait" in pages[0]


def run_ok(cfg, r):
    return runner.run(cfg, http=FakeHttp(r), today=TODAY)


# ---------------------------------------------------------------- conditional downloads
def test_unchanged_file_is_not_downloaded_again(tmp_path):
    cfg = config(tmp_path)
    pdf = make_pdf([AGENDA_P1, AGENDA_P2])
    r = routes(pdf)
    r[DALLAS_AGENDA] = Resp(pdf, etag='"v1"')

    first = FakeHttp(r)
    res = runner.run(cfg, http=first, today=TODAY)
    assert DALLAS_AGENDA not in first.conditional   # nothing to compare against yet
    assert res.new_matches

    second = FakeHttp(r)
    res = runner.run(cfg, http=second, today=TODAY)
    assert second.conditional[DALLAS_AGENDA] == ('"v1"', None)
    assert res.new_matches == [] and by_name(res)["Dallas"].unchanged == 2

    # The agenda is amended: the server sends the new file and a new ETag.
    amended = make_pdf([AGENDA_P1, AGENDA_P2 + "\nItem 15. DART Silver Line update"])
    r[DALLAS_AGENDA] = Resp(amended, etag='"v2"')
    res = runner.run(cfg, http=FakeHttp(r), today=TODAY)
    assert [m["term"] for m in res.new_matches] and all(m["term"] == "DART" for m in res.new_matches)
    store = Store(cfg["settings"]["database"])
    try:
        assert store.get_document(DALLAS_AGENDA)["etag"] == '"v2"'
    finally:
        store.close()


def test_last_modified_is_used_without_etag(tmp_path):
    cfg = config(tmp_path)
    pdf = make_pdf([AGENDA_P2])
    stamp = "Mon, 02 Nov 2026 15:00:00 GMT"
    r = routes(pdf)
    r[DALLAS_AGENDA] = Resp(pdf, last_modified=stamp)
    runner.run(cfg, http=FakeHttp(r), today=TODAY)
    again = FakeHttp(r)
    res = runner.run(cfg, http=again, today=TODAY)
    assert again.conditional[DALLAS_AGENDA] == (None, stamp)
    assert by_name(res)["Dallas"].unchanged == 2 and res.new_matches == []


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE documents (url TEXT PRIMARY KEY, source TEXT, city TEXT, body TEXT,
                  meeting_date TEXT, title TEXT, kind TEXT, detail_url TEXT, sha256 TEXT, pages INTEGER,
                  chars INTEGER, status TEXT, note TEXT, first_seen TEXT, last_checked TEXT)""")
    db.execute("INSERT INTO documents (url, status, sha256) VALUES ('https://x/a.pdf', 'ok', 'abc')")
    db.commit()
    db.close()

    store = Store(path)
    try:
        row = store.get_document("https://x/a.pdf")
        assert row["etag"] is None and row["last_modified"] is None and row["sha256"] == "abc"
    finally:
        store.close()
    Store(path).close()  # opening again is a no-op


# ---------------------------------------------------------------- Http.fetch
class FakeResponse:
    def __init__(self, status, body=b"", headers=None, url="https://x/a.pdf"):
        self.status_code, self._body, self.url = status, body, url
        self.headers = CaseInsensitiveDict(headers or {})

    def iter_content(self, chunk_size=None):
        if self._body:
            yield self._body

    def close(self):
        pass


@pytest.fixture
def http_with(monkeypatch):
    def make(response):
        http = Http(delay=0)
        sent = []

        def get(url, **kw):
            sent.append(kw.get("headers") or {})
            return response

        monkeypatch.setattr(http.session, "get", get)
        return http, sent
    return make


def test_fetch_returns_validators(http_with):
    http, sent = http_with(FakeResponse(200, b"%PDF-1.4", {"Content-Type": "application/pdf",
                                                           "ETag": '"abc"', "Last-Modified": "Mon"}))
    d = http.fetch("https://x/a.pdf")
    assert d.body == b"%PDF-1.4" and d.etag == '"abc"' and d.last_modified == "Mon" and not d.not_modified
    assert "If-None-Match" not in sent[0]


def test_fetch_sends_validators_and_handles_304(http_with):
    http, sent = http_with(FakeResponse(304, headers={"ETag": '"abc"'}))
    d = http.fetch("https://x/a.pdf", etag='"abc"', last_modified="Mon")
    assert d.not_modified and d.body == b""
    assert sent[0]["If-None-Match"] == '"abc"' and sent[0]["If-Modified-Since"] == "Mon"


def test_unasked_304_is_an_error(http_with):
    http, _ = http_with(FakeResponse(304))
    with pytest.raises(FetchError):
        http.fetch("https://x/a.pdf")
