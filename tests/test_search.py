"""Full-text search: index records from stored pages, the Pagefind build, and publishing the index."""

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from agendawatch import search
from agendawatch.runner import run
from agendawatch.search import search_records, sections_from_config, write_search_index
from agendawatch.storage import (IMMUTABLE_CACHE, SHORT_CACHE, AzureBlobStorage, LocalStorage, cache_control,
                                 is_hashed)

from helpers import FakeHttp, make_pdf

PAGE = "https://city.example/agendas"


def cfg(tmp_path):
    return {
        "settings": {"database": str(tmp_path / "t.db"), "report_dir": str(tmp_path / "r"),
                     "request_delay_seconds": 0, "lookback_days": 400, "lookahead_days": 60},
        "terms": [{"name": "DART", "case_sensitive": True}],
        "sources": [{"name": "City", "city": "Plano", "type": "link_page", "urls": [PAGE],
                     "link_pattern": r"/a/\d+\.pdf", "section": "DART member cities"}],
    }


def stored_db(tmp_path):
    http = FakeHttp({
        PAGE: '<table><tr><td>March 3, 2026 City Council</td><td><a href="/a/1.pdf">Agenda</a></td></tr>'
              '<tr><td>October 9, 2025 Planning and Zoning</td><td><a href="/a/2.pdf">Agenda</a></td></tr></table>',
        "https://city.example/a/1.pdf": make_pdf(["Item 1. Call to order", "Item 7. Bus stop on Belt Line   Road", " "]),
        "https://city.example/a/2.pdf": make_pdf(["Zoning case for a car wash"]),
    })
    c = cfg(tmp_path)
    run(c, http=http, today=date(2026, 3, 1))
    import sqlite3
    con = sqlite3.connect(c["settings"]["database"])
    with con:
        con.execute("UPDATE pages SET text=' \n ' WHERE page=3")    # a page with no text (a scan, a blank page)
    con.close()
    return c


def test_one_record_per_page_newest_first(tmp_path):
    c = stored_db(tmp_path)
    recs = list(search_records(c["settings"]["database"], sections_from_config(c)))
    assert [r["url"] for r in recs] == ["https://city.example/a/1.pdf#page=1", "https://city.example/a/1.pdf#page=2",
                                        "https://city.example/a/2.pdf"]      # blank page 3 skipped; 1-page PDF: no #page
    r = recs[1]
    assert r["content"] == "Item 7. Bus stop on Belt Line Road"
    assert r["meta"]["city"] == "Plano" and r["meta"]["date"] == "2026-03-03"
    assert r["meta"]["page"] == "2" and r["meta"]["pages"] == "3" and r["meta"]["section"] == "DART member cities"
    assert r["filters"] == {"city": ["Plano"], "section": ["DART member cities"], "year": ["2026"]}
    assert r["sort"] == {"date": "2026-03-03"}
    assert "page" not in recs[2]["meta"]


def test_missing_pagefind_skips_search(tmp_path, monkeypatch):
    c = stored_db(tmp_path)

    def unavailable():
        raise search.SearchUnavailable("the pagefind binary isn't installed")
    monkeypatch.setattr(search, "_pagefind", unavailable)
    site = tmp_path / "site"
    assert write_search_index(c["settings"]["database"], site, {}) is None
    assert not (site / "pagefind").exists()


def test_record_html_keeps_values_safe():
    rec = {"content": "Bus <stop> & shelter", "meta": {"title": "Council, Regular: 6 PM", "link": "https://x/a.pdf#page=2"},
           "filters": {"city": ["Plano"], "year": []}, "sort": {"date": "2026-03-03"}}
    html = search.record_html(rec)
    assert '<main data-pagefind-body>Bus &lt;stop&gt; &amp; shelter</main>' in html
    assert '<span data-pagefind-meta="title">Council, Regular: 6 PM</span>' in html
    assert '<span data-pagefind-filter="city">Plano</span>' in html and 'data-pagefind-filter="year"' not in html
    assert '<span data-pagefind-sort="date">2026-03-03</span>' in html


def fake_pagefind(monkeypatch, calls):
    """Stand in for the indexer: record which years were built and write a minimal bundle."""
    monkeypatch.setattr(search, "_pagefind", lambda: (Path("/bin/pagefind"), "1.5.2"))

    def build(binary, records, out, timeout):
        year = out.name
        calls.append((year, len(records)))
        if year == "fail":
            raise RuntimeError("boom")
        (out / "fragment").mkdir(parents=True, exist_ok=True)
        (out / "pagefind-entry.json").write_text("{}")
        for r in records:
            (out / "fragment" / f"en_{abs(hash(r['url'])) % 10**8}.pf_fragment").write_text(r["content"])
    monkeypatch.setattr(search, "_build_year", build)


def test_years_are_built_once_and_rebuilt_when_their_pages_change(tmp_path, monkeypatch):
    c = stored_db(tmp_path)
    db, site, calls = c["settings"]["database"], tmp_path / "site", []
    fake_pagefind(monkeypatch, calls)
    m = write_search_index(db, site, sections_from_config(c))
    assert calls == [("2026", 2), ("2025", 1)] and m["primary"] == "2026"
    assert m["built"] == ["2026", "2025"] and m["years"]["2026"]["pages"] == 2
    assert json.loads((site / "pagefind/manifest.json").read_text()) == m

    calls.clear()
    m2 = write_search_index(db, site, sections_from_config(c))           # nothing changed
    assert calls == [] and m2["built"] == [] and m2["years"] == m["years"]

    import sqlite3
    con = sqlite3.connect(db)
    with con:
        con.execute("UPDATE pages SET text='Item 7. Bus shelter on Belt Line' WHERE page=2")
    con.close()
    calls.clear()
    m3 = write_search_index(db, site, sections_from_config(c))
    assert calls == [("2026", 2)] and m3["built"] == ["2026"]              # only the year that changed
    assert m3["years"]["2025"] == m["years"]["2025"]


def test_a_failed_year_keeps_its_published_build(tmp_path, monkeypatch):
    c = stored_db(tmp_path)
    calls = []
    fake_pagefind(monkeypatch, calls)
    prev = {"years": {"2026": {"fp": "old", "pages": 9}}}
    monkeypatch.setattr(search, "year_of", lambda rec: "fail" if rec["sort"]["date"].startswith("2026") else "2025")
    m = write_search_index(c["settings"]["database"], tmp_path / "site", {}, previous=prev)
    assert m["built"] == ["2025"] and "fail" not in m["years"]

    def timeout(binary, records, out, t):
        raise subprocess.TimeoutExpired("pagefind", t)
    monkeypatch.setattr(search, "_build_year", timeout)
    monkeypatch.setattr(search, "year_of", lambda rec: rec["sort"]["date"][:4])
    m = write_search_index(c["settings"]["database"], tmp_path / "site2", {}, previous=prev)
    assert m["built"] == [] and m["years"] == {"2026": {"fp": "old", "pages": 9}}   # last build kept


def test_builds_real_pagefind_bundles(tmp_path):
    try:
        search._pagefind()
    except search.SearchUnavailable:
        pytest.skip("pagefind binary not installed")
    c = stored_db(tmp_path)
    site = tmp_path / "site"
    m = write_search_index(c["settings"]["database"], site, sections_from_config(c), timeout=300)
    assert m["built"] == ["2026", "2025"] and m["primary"] == "2026"
    for year, n in (("2026", 2), ("2025", 1)):
        out = site / "pagefind" / year
        assert (out / "pagefind.js").exists() and "en" in json.loads((out / "pagefind-entry.json").read_text())["languages"]
        frags = list((out / "fragment").iterdir())
        assert len(frags) == n and all(is_hashed(f.relative_to(site).as_posix()) for f in frags)
        # the page's link and date travel as metadata
        import gzip
        text = b"".join(gzip.decompress(f.read_bytes()) for f in frags).decode("utf-8", "replace")
        assert "city.example" in text and ("2026-03-03" in text or "2025-10-09" in text)


# ---------------------------------------------------------------- publishing the index
def fake_site(root: Path, years: dict[str, list[str]], built: list[str], listed: list[str] | None = None):
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    (root / "pagefind").mkdir(parents=True)
    (root / "index.html").write_text("<p>hi</p>")
    for year, frags in years.items():
        (root / "pagefind" / year / "fragment").mkdir(parents=True)
        (root / "pagefind" / year / "pagefind-entry.json").write_text("{}")
        (root / "pagefind" / year / "pagefind.js").write_text("//")
        for name in frags:
            (root / "pagefind" / year / "fragment" / name).write_text(name)
    listed = listed if listed is not None else list(years)
    (root / "pagefind/manifest.json").write_text(json.dumps(
        {"primary": listed[0] if listed else None, "years": {y: {"fp": y} for y in listed}, "built": built}))


def test_cache_rules():
    assert cache_control("pagefind/2026/fragment/en_abc.pf_fragment") == IMMUTABLE_CACHE
    assert cache_control("pagefind/2026/pagefind.en_abc.pf_meta") == IMMUTABLE_CACHE
    assert cache_control("pagefind/2026/pagefind-entry.json") == SHORT_CACHE
    assert cache_control("pagefind/2026/pagefind.js") == SHORT_CACHE     # changes when Pagefind is upgraded
    assert cache_control("pagefind/manifest.json") == SHORT_CACHE


def published(tmp_path):
    root = tmp_path / "hosted/site/pagefind"
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_publish_replaces_only_rebuilt_years(tmp_path):
    st = LocalStorage(tmp_path / "hosted")
    site = tmp_path / "site"
    fake_site(site, {"2026": ["a", "b"], "2025": ["c"]}, built=["2026", "2025"])
    st.publish_site(site)
    # Next run: 2026 rebuilt (b kept, a gone, d new); 2025 unchanged, so not in the build at all.
    fake_site(site, {"2026": ["b", "d"]}, built=["2026"], listed=["2026", "2025"])
    assert st.publish_site(site) == 5     # index.html, 2026's entry + pagefind.js + new fragment d, manifest
    assert published(tmp_path) == ["2025/fragment/c", "2025/pagefind-entry.json", "2025/pagefind.js",
                                   "2026/fragment/b", "2026/fragment/d", "2026/pagefind-entry.json",
                                   "2026/pagefind.js", "manifest.json"]
    # A year dropped from the manifest is removed.
    fake_site(site, {}, built=[], listed=["2026"])
    st.publish_site(site)
    assert not any(p.startswith("2025/") for p in published(tmp_path))


def test_a_run_without_a_manifest_leaves_the_published_index(tmp_path):
    st = LocalStorage(tmp_path / "hosted")
    site = tmp_path / "site"
    fake_site(site, {"2026": ["a"]}, built=["2026"])
    st.publish_site(site)
    import shutil
    shutil.rmtree(site / "pagefind")
    assert st.publish_site(site) == 1
    assert "2026/fragment/a" in published(tmp_path)
    assert st.get_site_file("pagefind/manifest.json") and st.get_site_file("nope") is None


class Blob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_blob(self, fh, overwrite=False, content_settings=None):
        self.store.blobs[self.name] = (fh.read(), content_settings.cache_control)
        self.store.uploads.append(self.name)

    def download_blob(self):
        from azure.core.exceptions import ResourceNotFoundError
        if self.name not in self.store.blobs:
            raise ResourceNotFoundError("missing")
        return type("D", (), {"readall": lambda _s: self.store.blobs[self.name][0]})()


class Container:
    def __init__(self):
        self.blobs, self.uploads = {}, []

    def get_blob_client(self, name):
        return Blob(self, name)

    def list_blob_names(self, name_starts_with=""):
        return [n for n in list(self.blobs) if n.startswith(name_starts_with)]

    def delete_blob(self, name):
        del self.blobs[name]


class Service:
    def __init__(self):
        self.containers = {}

    def get_container_client(self, name):
        return self.containers.setdefault(name, Container())


def test_azure_publish_syncs_the_index(tmp_path):
    pytest.importorskip("azure.storage.blob")
    svc = Service()
    st = AzureBlobStorage(service=svc)
    web = svc.containers["$web"]
    site = tmp_path / "site"
    fake_site(site, {"2026": ["en_a.pf_fragment", "en_b.pf_fragment"]}, built=["2026"])
    st.publish_site(site)
    assert web.blobs["pagefind/2026/fragment/en_a.pf_fragment"][1] == IMMUTABLE_CACHE
    assert web.uploads[-1] == "pagefind/manifest.json"                  # last, after what it points to
    web.uploads.clear()
    fake_site(site, {"2026": ["en_b.pf_fragment", "en_c.pf_fragment"]}, built=["2026"])
    st.publish_site(site)
    assert sorted(web.uploads) == ["index.html", "pagefind/2026/fragment/en_c.pf_fragment",
                                   "pagefind/2026/pagefind-entry.json", "pagefind/2026/pagefind.js",
                                   "pagefind/manifest.json"]
    assert "pagefind/2026/fragment/en_a.pf_fragment" not in web.blobs
    assert json.loads(st.get_site_file("pagefind/manifest.json"))["built"] == ["2026"]
    assert st.get_site_file("pagefind/nope.json") is None
