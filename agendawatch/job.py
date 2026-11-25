"""Hosted run: lock → fetch database → run → save database → publish site → unlock.

Used by `agenda_watch.py job` in the container. Settings can be overridden with environment
variables named AGENDAWATCH_SETTING_<NAME> (for example AGENDAWATCH_SETTING_SITE_URL or
AGENDAWATCH_SETTING_PAGE_RETENTION_DAYS=90); values are parsed as YAML, so numbers stay numbers.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import yaml

from .report import text_summary, write_report
from .search import sections_from_config, write_search_index
from .site import taxonomy_from_config
from .runner import run
from .storage import LockHeld, Storage
from .store import Store

log = logging.getLogger("agendawatch.job")

DB_KEY = "agenda_watch.db"
EXIT_OK, EXIT_ALL_SOURCES_FAILED, EXIT_LOCKED = 0, 3, 75

SETTING_PREFIX = "AGENDAWATCH_SETTING_"


def apply_env_overrides(cfg: dict, env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    settings = cfg.setdefault("settings", {})
    for key, raw in env.items():
        if key.startswith(SETTING_PREFIX) and len(key) > len(SETTING_PREFIX):
            name = key[len(SETTING_PREFIX):].lower()
            settings[name] = yaml.safe_load(raw) if raw != "" else None
            log.info("setting %s overridden from the environment", name)
    return cfg


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, for Log Analytics and similar."""

    def format(self, record):
        out = {"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
               "logger": record.name, "message": record.getMessage()}
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            out.update(fields)
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def run_job(cfg: dict, storage: Storage, today: date | None = None, http=None,
            backfill_days: int | None = None, only: list[str] | None = None) -> int:
    """`only` runs just the named sources and saves the database without publishing the site, since
    a page built from a partial run would list only those sources. The next full run publishes."""
    settings = cfg.setdefault("settings", {})
    terms = [t if isinstance(t, str) else t["name"] for t in cfg["terms"]]
    t0 = time.time()
    try:
        with storage.lock(), tempfile.TemporaryDirectory(prefix="agendawatch-") as tmp:
            work = Path(tmp)
            db = work / DB_KEY
            site = work / "site"
            found = storage.get_state(DB_KEY, db)
            log.info("database %s (%s)", "downloaded" if found else "not found, starting fresh",
                     f"{db.stat().st_size / 1e6:.1f} MB" if found else "first run")
            settings["database"] = str(db)
            settings["report_dir"] = str(site)

            if backfill_days:
                log.info("backfill: looking back %d days this run", backfill_days)
            if only:
                log.info("partial run: only %s; the site is not published", ", ".join(only))
            res = run(cfg, backfill_days=backfill_days, http=http, today=today, only=only)

            retention = settings.get("page_retention_days")
            if retention:
                store = Store(db)
                try:
                    cutoff = (today or date.today()) - timedelta(days=int(retention))
                    pruned = store.prune_pages(cutoff)
                finally:
                    store.close()
                log.info("pruned page text of %d agenda(s) older than %s", pruned, cutoff)

            if only:
                storage.put_state(DB_KEY, db)
                log.info("partial run saved; database %.1f MB", db.stat().st_size / 1e6,
                         extra={"fields": {"event": "saved_partial", "db_mb": round(db.stat().st_size / 1e6, 1)}})
                n = 0
            else:
                write_report(res, terms, site, settings.get("site_url"), taxonomy_from_config(cfg))
                # Save the database before publishing: if publishing fails, the next run republishes;
                # if saving failed after publishing, the next run would report the same items as new again.
                # (And before the search index, the slowest step, so a crash there loses nothing.)
                storage.put_state(DB_KEY, db)
                if settings.get("search_index", True):
                    write_search_index(db, site, sections_from_config(cfg))
                n = storage.publish_site(site)
            if not only:
                log.info("published %d site file(s); database %.1f MB", n, db.stat().st_size / 1e6,
                         extra={"fields": {"event": "published", "files": n, "db_mb": round(db.stat().st_size / 1e6, 1)}})
    except LockHeld as exc:
        log.error("another run is still in progress: %s", exc, extra={"fields": {"event": "locked"}})
        return EXIT_LOCKED

    for line in text_summary(res).splitlines():
        log.info(line)
    ok = [s for s in res.statuses if not s.error]
    log.info("run finished in %.0f s: %d/%d sources OK, %d new match(es)", time.time() - t0, len(ok),
             len(res.statuses), len(res.new_matches),
             extra={"fields": {"event": "run_finished", "seconds": round(time.time() - t0), "sources_ok": len(ok),
                               "sources": len(res.statuses), "new_matches": len(res.new_matches)}})
    if res.statuses and not ok:
        log.error("every source failed — likely a network problem", extra={"fields": {"event": "all_sources_failed"}})
        return EXIT_ALL_SOURCES_FAILED
    return EXIT_OK
