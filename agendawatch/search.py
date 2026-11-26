"""Full-text search: Pagefind indexes of every stored agenda page, published with the site.

The site is static, so the index is too. Pagefind splits an index into small files (one per agenda
page, plus index chunks keyed by word) and the browser fetches only those a search needs. One record
per PDF page means a result links straight to the page (`agenda.pdf#page=N`) and a hit in a 300-page
packet downloads one page of text, not three hundred.

The index is built in batches, one Pagefind bundle per meeting year (`pagefind/<year>/`), and the
page merges them when searching (Pagefind's mergeIndex). Each year's pages are fingerprinted; a year
whose pages haven't changed since the published build isn't rebuilt or re-uploaded, so a daily run
usually rebuilds only the current year. `pagefind/manifest.json` lists the years, their
fingerprints, and which were built this run (storage.publish_site uses it to know what to replace).

Each year is built by the Pagefind command-line indexer over a folder of small HTML files, one per
page, under a time limit. A year that fails or runs out of time keeps its previously published
bundle. Needs the `pagefind` binary (`pip install "pagefind[bin]"`); without it, search is skipped
and the rest of the site is published as usual.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from html import escape
from itertools import groupby
from pathlib import Path
from typing import Iterator

from .feeds import OTHER
from .site import _page_link
from .storage import SEARCH_DIR, SEARCH_MANIFEST

log = logging.getLogger("agendawatch.search")

FORMAT = 2                 # bump when the records or HTML change, to rebuild every year
UNDATED = "undated"        # bundle for agendas without a meeting date
_SPACE = re.compile(r"[ \t\f\v\r]+")


class SearchUnavailable(RuntimeError):
    """The pagefind binary isn't installed."""


def search_records(database: str | Path, section_of: dict[str, str]) -> Iterator[dict]:
    """One record per stored page with text: what Pagefind indexes, newest meetings first."""
    con = sqlite3.connect(f"file:{Path(database)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT d.url, d.source, d.city, d.body, d.title, d.kind, d.meeting_date, d.pages, d.detail_url,
                      p.page, p.text
               FROM pages p JOIN documents d ON d.url = p.url
               WHERE d.status = 'ok' AND p.text IS NOT NULL
               ORDER BY d.meeting_date DESC, d.url, p.page""")
        for r in rows:
            content = _SPACE.sub(" ", r["text"]).strip()
            if not content:          # a blank page, or a scan OCR couldn't read
                continue
            day = r["meeting_date"] or ""
            pages = int(r["pages"] or 0)
            kind = r["kind"] or "agenda"
            link = _page_link(r["url"], r["page"] if pages > 1 else None, kind)
            meta = {"title": r["body"] or r["title"] or "Meeting", "city": r["city"] or "", "date": day,
                    "kind": kind, "section": section_of.get(r["source"], OTHER), "link": link}
            if pages > 1 and kind != "items":
                meta.update(page=str(r["page"]), pages=str(pages))
            if r["detail_url"]:
                meta["detail"] = r["detail_url"]
            yield {
                "url": link,
                "content": content,
                "meta": meta,
                "filters": {"city": [meta["city"]], "section": [meta["section"]], "year": [day[:4]] if day else []},
                "sort": {"date": day},
            }
    finally:
        con.close()


def year_of(rec: dict) -> str:
    return rec["sort"]["date"][:4] or UNDATED


def record_html(rec: dict) -> str:
    """A page for the Pagefind indexer: the text is the content; meta, filters and sort sit outside
    the indexed body, given as element content so commas and colons in values are safe."""
    tags = [f'<span data-pagefind-meta="{k}">{escape(v)}</span>' for k, v in rec["meta"].items()]
    tags += [f'<span data-pagefind-filter="{k}">{escape(v)}</span>' for k, vs in rec["filters"].items() for v in vs]
    tags += [f'<span data-pagefind-sort="{k}">{escape(v)}</span>' for k, v in rec["sort"].items() if v]
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(rec["meta"]["title"])}</title></head>'
            f'<body><div>{"".join(tags)}</div><main data-pagefind-body>{escape(rec["content"])}</main></body></html>')


def _pagefind() -> tuple[Path, str]:
    """The pagefind binary and its version."""
    from importlib import metadata

    for pkg in ("pagefind_bin", "pagefind_bin_extended"):
        try:
            mod = __import__(pkg)
            return Path(mod.get_executable()), metadata.version(pkg.replace("_", "-"))
        except (ImportError, metadata.PackageNotFoundError):
            continue
    found = shutil.which("pagefind") or shutil.which("pagefind_extended")
    if found:
        return Path(found), "external"
    raise SearchUnavailable("the pagefind binary isn't installed")


def fingerprint(records: list[dict], version: str) -> str:
    h = hashlib.sha256(f"{FORMAT}|{version}".encode())
    for r in records:
        h.update(json.dumps(r, sort_keys=True, ensure_ascii=False).encode())
        h.update(b"\0")
    return h.hexdigest()[:24]


def _build_year(binary: Path, records: list[dict], out: Path, timeout: float) -> None:
    with tempfile.TemporaryDirectory(prefix="agendawatch-search-") as tmp:
        site = Path(tmp) / "site"
        for i, rec in enumerate(records):
            d = site / "p" / str(i // 1000)
            if i % 1000 == 0:
                d.mkdir(parents=True)
            (d / f"{i}.html").write_text(record_html(rec), encoding="utf-8")
        built = Path(tmp) / "out"
        proc = subprocess.run([str(binary), "--site", str(site), "--output-path", str(built),
                               "--force-language", "en", "--quiet"],
                              capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 or not (built / "pagefind-entry.json").exists():
            raise RuntimeError(f"pagefind exited {proc.returncode}: {(proc.stdout + proc.stderr)[-500:]}")
        shutil.rmtree(out, ignore_errors=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(built), str(out))


def read_manifest(raw: bytes | str | None) -> dict:
    try:
        m = json.loads(raw) if raw else {}
    except ValueError:
        m = {}
    return m if isinstance(m, dict) and isinstance(m.get("years"), dict) else {"years": {}}


def write_search_index(database: str | Path, site_dir: str | Path, section_of: dict[str, str],
                       previous: dict | None = None, timeout: float = 1200) -> dict | None:
    """Build site_dir/pagefind/<year>/ for each year whose pages changed since `previous` (the
    published manifest; read from site_dir when not given), and write the manifest. Returns the
    manifest, or None if search isn't available here."""
    out = Path(site_dir) / SEARCH_DIR
    if previous is None:
        mf = out / SEARCH_MANIFEST.split("/", 1)[1]
        previous = read_manifest(mf.read_bytes() if mf.exists() else None)
    try:
        binary, version = _pagefind()
    except SearchUnavailable as exc:
        log.warning("full-text search index not built: %s", exc)
        return None
    t0 = time.time()
    years: dict[str, dict] = {}
    built: list[str] = []
    for year, group in groupby(search_records(database, section_of), key=year_of):
        records = list(group)
        fp = fingerprint(records, version)
        old = previous["years"].get(year)
        if old and old.get("fp") == fp:
            years[year] = old
            log.info("search index %s: %d page(s) unchanged", year, len(records))
            continue
        t = time.time()
        try:
            _build_year(binary, records, out / year, timeout)
        except Exception as exc:  # noqa: BLE001 - one year failing mustn't stop the rest
            msg = "timed out" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)[:300]
            if old:
                years[year] = old
            log.warning("search index %s: not rebuilt (%s); %s", year, msg,
                        "the published one stays" if old else "it's left out",
                        extra={"fields": {"event": "search_index_failed", "year": year}})
            continue
        years[year] = {"fp": fp, "pages": len(records)}
        built.append(year)
        files = sum(1 for p in (out / year).rglob("*") if p.is_file())
        log.info("search index %s: %d page(s) built in %.0f s, %d file(s)", year, len(records), time.time() - t, files,
                 extra={"fields": {"event": "search_year", "year": year, "pages": len(records), "files": files,
                                   "seconds": round(time.time() - t)}})
    dated = sorted((y for y in years if y != UNDATED), reverse=True)
    manifest = {"format": FORMAT, "pagefind": version, "primary": (dated or list(years) or [None])[0],
                "years": {y: years[y] for y in dated + [y for y in years if y == UNDATED]}, "built": built}
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    log.info("search index: %d page(s) in %d year(s), %d rebuilt, %.0f s",
             sum(y["pages"] for y in years.values()), len(years), len(built), time.time() - t0,
             extra={"fields": {"event": "search_index", "pages": sum(y["pages"] for y in years.values()),
                               "years": len(years), "built": len(built), "seconds": round(time.time() - t0)}})
    return manifest


def sections_from_config(cfg: dict) -> dict[str, str]:
    return {s["name"]: s.get("section") or OTHER for s in cfg.get("sources", []) if isinstance(s, dict)}
