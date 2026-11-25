"""Full-text search: index records from stored pages, the Pagefind build, and publishing the index."""

import json
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


def test_missing_pagefind_skips_search_and_leaves_no_files(tmp_path, monkeypatch):
    c = stored_db(tmp_path)

    async def unavailable(records, out):
        raise search.SearchUnavailable("the pagefind package isn't installed")
    monkeypatch.setattr(search, "_build", unavailable)
    site = tmp_path / "site"
    assert write_search_index(c["settings"]["database"], site, {}) == 0
    assert not (site / "pagefind").exists() and not (site / "pagefind.building").exists()


def test_builds_a_pagefind_bundle(tmp_path):
    pytest.importorskip("pagefind")
    c = stored_db(tmp_path)
    site = tmp_path / "site"
    (site / "pagefind" / "old").mkdir(parents=True)          # yesterday's bundle is replaced
    assert write_search_index(c["settings"]["database"], site, sections_from_config(c)) == 3
    out = site / "pagefind"
    assert (out / "pagefind.js").exists() and not (out / "old").exists()
    entry = json.loads((out / "pagefind-entry.json").read_text())
    assert "en" in entry["languages"]
    assert len(list((out / "fragment").iterdir())) == 3
    assert all(is_hashed(p.relative_to(site).as_posix()) for p in (out / "fragment").iterdir())


# ---------------------------------------------------------------- publishing the index
def fake_site(root: Path, fragments: list[str]):
    (root / "pagefind" / "fragment").mkdir(parents=True, exist_ok=True)
    for f in (root / "pagefind" / "fragment").iterdir():
        f.unlink()
    (root / "index.html").write_text("<p>hi</p>")
    (root / "pagefind" / "pagefind-entry.json").write_text("{}")
    (root / "pagefind" / "pagefind.js").write_text("//")
    for name in fragments:
        (root / "pagefind" / "fragment" / name).write_text(name)


def test_cache_rules():
    assert cache_control("pagefind/fragment/en_abc.pf_fragment") == IMMUTABLE_CACHE
    assert cache_control("pagefind/pagefind.en_abc.pf_meta") == IMMUTABLE_CACHE
    assert cache_control("pagefind/pagefind-entry.json") == SHORT_CACHE
    assert cache_control("pagefind/pagefind.js") == SHORT_CACHE     # changes when Pagefind is upgraded


def test_publish_skips_published_index_files_and_removes_stale_ones(tmp_path):
    st = LocalStorage(tmp_path / "hosted")
    site = tmp_path / "site"
    fake_site(site, ["en_a.pf_fragment", "en_b.pf_fragment"])
    assert st.publish_site(site) == 5
    fake_site(site, ["en_b.pf_fragment", "en_c.pf_fragment"])
    assert st.publish_site(site) == 4          # index.html, entry, pagefind.js, the new fragment c
    published = tmp_path / "hosted/site/pagefind/fragment"
    assert sorted(p.name for p in published.iterdir()) == ["en_b.pf_fragment", "en_c.pf_fragment"]


def test_a_run_without_an_index_leaves_the_published_one(tmp_path):
    st = LocalStorage(tmp_path / "hosted")
    site = tmp_path / "site"
    fake_site(site, ["en_a.pf_fragment"])
    st.publish_site(site)
    import shutil
    shutil.rmtree(site / "pagefind")
    assert st.publish_site(site) == 1
    assert (tmp_path / "hosted/site/pagefind/fragment/en_a.pf_fragment").exists()


class Blob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_blob(self, fh, overwrite=False, content_settings=None):
        self.store.blobs[self.name] = (fh.read(), content_settings.cache_control)
        self.store.uploads.append(self.name)


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
    fake_site(site, ["en_a.pf_fragment", "en_b.pf_fragment"])
    st.publish_site(site)
    assert web.blobs["pagefind/fragment/en_a.pf_fragment"][1] == IMMUTABLE_CACHE
    web.uploads.clear()
    fake_site(site, ["en_b.pf_fragment", "en_c.pf_fragment"])
    st.publish_site(site)
    assert sorted(web.uploads) == ["index.html", "pagefind/fragment/en_c.pf_fragment",
                                   "pagefind/pagefind-entry.json", "pagefind/pagefind.js"]
    assert "pagefind/fragment/en_a.pf_fragment" not in web.blobs
