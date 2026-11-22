"""Daily run: list agendas, download new or changed ones, search them, record matches."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .extract import extract_pages
from .http import FetchError, Http
from .matcher import compile_terms, find_hits
from .sources import Document, build_sources
from .store import Store

log = logging.getLogger("agendawatch")


@dataclass
class SourceStatus:
    name: str
    city: str
    platform: str
    listed: int = 0
    read: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    new_matches: int = 0
    error: str = ""
    section: str = ""
    notes: list[str] = field(default_factory=list)
    # health, across runs (Store.source_health)
    last_new: str | None = None          # date this source last produced a new agenda
    stale: bool = False                  # no new agenda for longer than stale_after_days
    stale_after_days: int | None = None
    lead_days: float | None = None       # median days of notice (meeting date − first seen)
    lead_samples: int = 0
    failing_runs: int = 0                # consecutive runs ending with an error, including this one
    failing_since: str | None = None


@dataclass
class RunResult:
    run_id: int
    start: date
    end: date
    statuses: list[SourceStatus]
    new_matches: list
    upcoming: list
    previous_run: str | None
    window_matches: list = field(default_factory=list)   # every match for meetings in start..end
    run_started: str | None = None
    today: date | None = None


def make_http(settings: dict) -> Http:
    return Http(
        delay=float(settings.get("request_delay_seconds", 1.0)),
        timeout=float(settings.get("timeout_seconds", 60)),
        max_bytes=int(float(settings.get("max_download_mb", 80)) * 1024 * 1024),
    )


def _process(doc: Document, store: Store, http: Http, terms, settings: dict, run_id: int, st: SourceStatus, today: date) -> None:
    existing = store.get_document(doc.url)

    # Past meetings we've already read don't change; skip the download.
    recheck_after = today - timedelta(days=int(settings.get("recheck_past_days", 2)))
    if existing and existing["status"] == "ok" and doc.meeting_date and doc.meeting_date < recheck_after:
        st.skipped += 1
        return

    try:
        if doc.inline_text is not None:
            data, ctype = doc.inline_text.encode("utf-8"), "text/plain"
        else:
            data, ctype, _ = http.download(doc.url)
    except FetchError as exc:
        st.failed += 1
        store.save_document(doc, None, None, "error", str(exc))
        log.warning("  ! %s: %s", doc.title, exc)
        return

    sha = hashlib.sha256(data).hexdigest()
    if existing and existing["status"] == "ok" and existing["sha256"] == sha:
        st.unchanged += 1
        store.save_document(doc, sha, None, "ok", existing["note"] or "")
        return

    pages, note = extract_pages(data, ctype, ocr=str(settings.get("ocr", "auto")))
    store.save_document(doc, sha, pages, "ok", note)
    st.read += 1
    if note and "scanned" in note:
        st.notes.append(f"{doc.title} ({doc.meeting_date}): {note}")
    elif doc.inline_text is None and sum(len(p) for p in pages) < 200:
        # Usually a link that returns an interstitial or error page instead of the agenda
        # (Frisco's "Downloading, please wait…" page was one).
        st.notes.append(f"{doc.title} ({doc.meeting_date}): almost no text ({sum(len(p) for p in pages)} characters) — check the link")

    hits, _counts = find_hits(
        pages, terms,
        context=int(settings.get("snippet_chars", 140)),
        max_per_term=int(settings.get("max_snippets_per_term", 5)),
    )
    scope = f"{doc.city}|{doc.meeting_date}"
    new = store.add_hits(doc.url, hits, run_id, scope)
    st.new_matches += new
    if new:
        log.info("  + %d new match(es) in %s %s (%s)", new, doc.city, doc.title, doc.meeting_date)


def _apply_health(statuses: list[SourceStatus], health: dict, today: date, default_stale: int) -> None:
    for st in statuses:
        h = health.get(st.name) or {}
        st.last_new = h.get("last_new")
        st.lead_days = h.get("lead_days")
        st.lead_samples = h.get("lead_samples", 0)
        st.failing_runs = h.get("failing_runs", 0)
        st.failing_since = h.get("failing_since")
        limit = st.stale_after_days if st.stale_after_days is not None else default_stale
        st.stale_after_days = limit
        first_ever = h.get("first_ever")
        # Only judge sources we've watched for longer than the limit, and that are being read at all
        # (a source that errors is reported as failing instead).
        if limit and not st.error and first_ever and st.last_new:
            old_enough = (today - date.fromisoformat(first_ever)).days > limit
            st.stale = old_enough and (today - date.fromisoformat(st.last_new)).days > limit


def run(config: dict, backfill_days: int | None = None, http: Http | None = None, today: date | None = None) -> RunResult:
    settings = config.get("settings", {}) or {}
    terms = compile_terms(config["terms"])
    http = http or make_http(settings)
    sources = build_sources(config["sources"], http)
    real_today = date.today()
    today = today or real_today
    # A run dated another day (tests, backfills) stamps "first seen" on that day too, so lead times
    # and staleness stay consistent.
    clock = None if today == real_today else (lambda: datetime.combine(today, datetime.now().time()).isoformat(timespec="seconds"))
    store = Store(settings.get("database", "data/agenda_watch.db"), now=clock)

    lookback = backfill_days if backfill_days is not None else int(settings.get("lookback_days", 10))
    start = today - timedelta(days=lookback)
    end = today + timedelta(days=int(settings.get("lookahead_days", 60)))

    run_id = store.start_run()
    statuses: list[SourceStatus] = []
    try:
        for src in sources:
            st = SourceStatus(src.name, src.city, src.platform, section=src.section,
                              stale_after_days=src.stale_after_days)
            statuses.append(st)
            if src.note:
                st.notes.append(src.note)
            log.info("%s (%s)", src.name, src.platform)
            try:
                docs = src.list_documents(start, end)
            except Exception as exc:  # noqa: BLE001 - one broken site shouldn't stop the others
                st.error = f"{type(exc).__name__}: {exc}"
                log.warning("  ! couldn't list agendas: %s", st.error)
                continue
            finally:
                st.notes.extend(src.notes)
            st.listed = len(docs)
            log.info("  %d agenda(s) in window", len(docs))
            for doc in docs:
                _process(doc, store, http, terms, settings, run_id, st, today)
        store.finish_run(run_id)
        for st in statuses:
            store.record_source_run(run_id, st)
        _apply_health(statuses, store.source_health(today, int(settings.get("lead_window_days", 180))),
                      today, int(settings.get("stale_after_days", 30)))
        for st in statuses:
            log.info(
                "source %s: listed=%d read=%d unchanged=%d skipped=%d failed=%d new=%d%s",
                st.name, st.listed, st.read, st.unchanged, st.skipped, st.failed, st.new_matches,
                f" error={st.error}" if st.error else "",
                extra={"fields": {"event": "source", "source": st.name, "section": st.section, "listed": st.listed,
                                  "read": st.read, "unchanged": st.unchanged, "skipped": st.skipped,
                                  "failed": st.failed, "new_matches": st.new_matches, "error": st.error or None,
                                  "last_new": st.last_new, "stale": st.stale, "lead_days": st.lead_days,
                                  "failing_runs": st.failing_runs}},
            )
            if st.stale:
                log.warning("source %s looks stale: no new agenda since %s", st.name, st.last_new,
                            extra={"fields": {"event": "stale_source", "source": st.name, "last_new": st.last_new}})
            if st.failing_runs >= 3:
                log.warning("source %s has failed %d runs in a row (since %s)", st.name, st.failing_runs,
                            st.failing_since, extra={"fields": {"event": "failing_source", "source": st.name,
                                                                "failing_runs": st.failing_runs}})
        return RunResult(
            run_id=run_id, start=start, end=end, statuses=statuses,
            new_matches=store.matches_for_run(run_id),
            upcoming=store.upcoming_with_matches(today),
            previous_run=store.previous_run_time(run_id),
            window_matches=store.matches_in_window(start, end),
            run_started=store.run_started(run_id),
            today=today,
        )
    finally:
        store.close()


def check(config: dict, http: Http | None = None) -> list[tuple[str, str, list[Document], str]]:
    """List what each source finds without downloading anything."""
    settings = config.get("settings", {}) or {}
    http = http or make_http(settings)
    today = date.today()
    start = today - timedelta(days=int(settings.get("lookback_days", 10)))
    end = today + timedelta(days=int(settings.get("lookahead_days", 60)))
    out = []
    for src in build_sources(config["sources"], http):
        try:
            docs = src.list_documents(start, end)
            msg = "; ".join(src.notes)
        except Exception as exc:  # noqa: BLE001
            docs, msg = [], f"ERROR {type(exc).__name__}: {exc}"
        out.append((src.name, src.platform, docs, msg))
    return out
