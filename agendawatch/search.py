"""Full-text search: a Pagefind index of every stored agenda page, published with the site.

The site is static, so the index is too. Pagefind splits it into small files (one per agenda page,
plus index chunks keyed by word) and the browser fetches only those a search needs. One record per
PDF page means a result links straight to the page (`agenda.pdf#page=N`) and a hit in a 300-page
packet downloads one page of text, not three hundred.

The index is rebuilt from the database each run. Pagefind names most of its files by a hash of
their content, so publishing uploads only the pages that are new (see storage.publish_site).

Needs the `pagefind` package with its binary (`pip install "pagefind[bin]"`). Without it, the site is
written without search and the page says so.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from .feeds import OTHER
from .site import _page_link
from .storage import SEARCH_DIR

log = logging.getLogger("agendawatch.search")

BATCH = 500          # records in flight to the Pagefind service at once
_SPACE = re.compile(r"[ \t\f\v\r]+")


class SearchUnavailable(RuntimeError):
    """The pagefind package or its binary isn't installed."""


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
                    "kind": kind, "section": section_of.get(r["source"], OTHER)}
            if pages > 1 and kind != "items":
                meta.update(page=str(r["page"]), pages=str(pages))
            if r["detail_url"]:
                meta["detail"] = r["detail_url"]
            yield {
                "url": link,
                "content": content,
                "language": "en",
                "meta": meta,
                "filters": {"city": [meta["city"]], "section": [meta["section"]], "year": [day[:4]] if day else []},
                "sort": {"date": day},
            }
    finally:
        con.close()


async def _build(records: Iterator[dict], out: Path) -> int:
    try:
        from pagefind.index import IndexConfig, PagefindIndex
    except ImportError as exc:
        raise SearchUnavailable("the pagefind package isn't installed") from exc
    n = 0
    try:
        async with PagefindIndex(IndexConfig(output_path=str(out), force_language="en")) as ix:
            batch: list[dict] = []
            for rec in records:
                batch.append(rec)
                if len(batch) >= BATCH:
                    await asyncio.gather(*(ix.add_custom_record(**r) for r in batch))
                    n += len(batch)
                    batch = []
            if batch:
                await asyncio.gather(*(ix.add_custom_record(**r) for r in batch))
                n += len(batch)
    except FileNotFoundError as exc:     # the binary couldn't be found
        raise SearchUnavailable(str(exc)) from exc
    return n


def write_search_index(database: str | Path, site_dir: str | Path, section_of: dict[str, str]) -> int:
    """(Re)build site_dir/pagefind from the database. Returns the number of pages indexed, or 0
    (and logs why) if search isn't available here."""
    out = Path(site_dir) / SEARCH_DIR
    tmp = out.with_name(SEARCH_DIR + ".building")
    shutil.rmtree(tmp, ignore_errors=True)
    t0 = time.time()
    try:
        n = asyncio.run(_build(search_records(database, section_of), tmp))
    except SearchUnavailable as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        log.warning("full-text search index not built: %s", exc)
        return 0
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        log.exception("full-text search index failed; the site is published without it")
        return 0
    shutil.rmtree(out, ignore_errors=True)
    tmp.replace(out)
    files = sum(1 for p in out.rglob("*") if p.is_file())
    mb = sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) / 1e6
    log.info("search index: %d page(s) in %d file(s), %.1f MB, %.0f s", n, files, mb, time.time() - t0,
             extra={"fields": {"event": "search_index", "pages": n, "files": files, "mb": round(mb, 1),
                               "seconds": round(time.time() - t0)}})
    return n


def sections_from_config(cfg: dict) -> dict[str, str]:
    return {s["name"]: s.get("section") or OTHER for s in cfg.get("sources", []) if isinstance(s, dict)}
