"""Daily run: list agendas, download new or changed ones, search them, record matches."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import feeds as feed_paths
from .extract import extract_pages
from .http import FetchError, Http
from .matcher import MATCHER_VERSION, compile_terms, find_hits
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
    agenda_page: str = ""                # the city's own agenda page, for people


@dataclass
class RunResult:
    run_id: int
    start: date
    end: date
    statuses: list[SourceStatus]
    new_matches: list
    upcoming: list
    previous_run: str | None
    window_matches: list = field(default_factory=list)   # every match for meetings in report_start..end
    archive_matches: list = field(default_factory=list)  # every match for meetings before report_start
    report_start: date | None = None     # the report shows meetings from here (≤ start; see report_lookback_days)
    run_started: str | None = None
    today: date | None = None
    feeds: list = field(default_factory=list)            # per-city/section feeds to publish: {kind, name, path}
    retired_feeds: list = field(default_factory=list)    # published before, no longer watched (feeds.py)


def make_http(settings: dict) -> Http:
    return Http(
        delay=float(settings.get("request_delay_seconds", 1.0)),
        timeout=float(settings.get("timeout_seconds", 60)),
        max_bytes=int(float(settings.get("max_download_mb", 80)) * 1024 * 1024),
    )


_UNREADABLE = ("unsupported file type", "unreadable", "install ocrmypdf")


def _process(doc: Document, store: Store, http: Http, terms, settings: dict, run_id: int, st: SourceStatus, today: date) -> None:
    existing = store.get_document(doc.url)

    # Past meetings we've already read don't change; skip the download.
    recheck_after = today - timedelta(days=int(settings.get("recheck_past_days", 2)))
    if existing and existing["status"] == "ok" and doc.meeting_date and doc.meeting_date < recheck_after:
        st.skipped += 1
        return

    # A file we couldn't read last time (an unsupported format or a scanned PDF without OCR) is
    # downloaded and read again even if unchanged: a newer version of the tool may do better.
    unreadable_before = any(k in (existing["note"] or "") for k in _UNREADABLE) if existing else False
    read_before = bool(existing and existing["status"] == "ok" and existing["sha256"] and not unreadable_before)

    validators = None
    try:
        if doc.inline_text is not None:
            data, ctype = doc.inline_text.encode("utf-8"), "text/plain"
        else:
            # Ask the server to skip the body if the file hasn't changed since we read it.
            dl = http.fetch(doc.url,
                            etag=existing["etag"] if read_before else None,
                            last_modified=existing["last_modified"] if read_before else None)
            if dl.not_modified:
                st.unchanged += 1
                # Servers may refresh validators on a 304; keep ours for any they don't resend.
                store.save_document(doc, None, None, "ok", existing["note"] or "",
                                    validators=(dl.etag or existing["etag"],
                                                dl.last_modified or existing["last_modified"]))
                return
            data, ctype, validators = dl.body, dl.content_type, (dl.etag, dl.last_modified)
    except FetchError as exc:
        st.failed += 1
        store.save_document(doc, None, None, "error", str(exc))
        log.warning("  ! %s: %s", doc.title, exc)
        return

    sha = hashlib.sha256(data).hexdigest()
    if read_before and existing["sha256"] == sha:
        st.unchanged += 1
        store.save_document(doc, sha, None, "ok", existing["note"] or "", validators=validators)
        return

    try:
        pages, note = extract_pages(data, ctype, ocr=str(settings.get("ocr", "auto")))
    except Exception as exc:  # noqa: BLE001 - a corrupt or mislabeled file shouldn't stop the run
        # Saved as an error, so the next run downloads and tries it again.
        st.failed += 1
        msg = f"couldn't read the file: {type(exc).__name__}: {exc}"
        store.save_document(doc, None, None, "error", msg)
        st.notes.append(f"{doc.title} ({doc.meeting_date}): {msg}")
        log.warning("  ! %s: %s", doc.title, msg)
        return
    store.save_document(doc, sha, pages, "ok", note, validators=validators)
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
    # Agendas of meetings older than the report window (archive backfills) are stored and
    # searchable, but their matches aren't news: they don't count as new in this run or the feeds.
    archive_before = today - timedelta(days=int(settings.get("report_lookback_days", 30)))
    if doc.meeting_date and doc.meeting_date < archive_before:
        added = store.add_hits(doc.url, hits, 0, scope, first_seen=f"{doc.meeting_date.isoformat()}T00:00:00")
        st.read += 1
        if added:
            log.info("  + %d archived match(es) in %s %s (%s)", added, doc.city, doc.title, doc.meeting_date)
        return
    new = store.add_hits(doc.url, hits, run_id, scope)
    st.read += 1   # counted here so a failure above counts as failed only
    st.new_matches += new
    if new:
        log.info("  + %d new match(es) in %s %s (%s)", new, doc.city, doc.title, doc.meeting_date)


def _contain(exc: Exception, doc: Document, store: Store, st: SourceStatus) -> None:
    """Record an unexpected per-document failure so the document is downloaded and read again next run."""
    st.failed += 1
    msg = f"{type(exc).__name__}: {exc}"
    st.notes.append(f"{doc.title} ({doc.meeting_date}): {msg}")
    log.exception("  ! %s: unexpected error", doc.title)
    # The document may already be saved as read (sha and validators stored) before its matches were;
    # left that way, the next run would see it as unchanged and never record them.
    store.db.rollback()
    try:
        store.save_document(doc, None, None, "error", msg)
    except Exception:  # noqa: BLE001 - don't let the bookkeeping hide the original error
        store.db.rollback()
        log.exception("  ! %s: couldn't record the error", doc.title)


def terms_fingerprint(raw_terms: list) -> str:
    """Changes whenever the term list or the matcher does (tags don't matter: they're applied at report time)."""
    def strip(t):
        return t if isinstance(t, str) else {k: v for k, v in t.items() if k != "tags"}
    basis = json.dumps({"v": MATCHER_VERSION, "terms": [strip(t) for t in raw_terms]}, sort_keys=True, default=str)
    return hashlib.sha1(basis.encode()).hexdigest()


def rematch_stored(store: Store, terms, settings: dict, run_id: int, today: date) -> tuple[int, int, int]:
    """Re-match every agenda whose text is still stored, after the term list or matcher changed.

    Matches that no longer apply are removed (noise rules, overlaps). New matches on upcoming
    meetings count as new in this run; on past meetings they're back-dated to when the agenda was
    first seen, so feeds and "new since your last visit" aren't flooded with old meetings.
    Returns (documents, matches added, matches removed)."""
    docs = added = removed = 0
    for row in store.documents_with_text():
        hits, _ = find_hits(store.page_texts(row["url"]), terms,
                            context=int(settings.get("snippet_chars", 140)),
                            max_per_term=int(settings.get("max_snippets_per_term", 5)))
        scope = f"{row['city']}|{row['meeting_date']}"
        removed += store.drop_hits_except(row["url"], {f"{scope}|{h.key}" for h in hits})
        past = bool(row["meeting_date"]) and row["meeting_date"] < today.isoformat()
        added += store.add_hits(row["url"], hits, 0 if past else run_id, scope,
                                first_seen=row["first_seen"] if past else None)
        docs += 1
    return docs, added, removed


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


def select_sources(specs: list[dict], only: list[str] | None) -> list[dict]:
    """The source specs named in `only` (all of them when it's empty); unknown names are an error."""
    if not only:
        return specs
    names = {s["name"] for s in specs}
    missing = [n for n in only if n not in names]
    if missing:
        raise ValueError(f"No source named {missing} in the config")
    return [s for s in specs if s["name"] in only]


def run(config: dict, backfill_days: int | None = None, http: Http | None = None, today: date | None = None,
        only: list[str] | None = None) -> RunResult:
    """`only` limits the run to the named sources (for archive backfills)."""
    settings = config.get("settings", {}) or {}
    terms = compile_terms(config["terms"])
    http = http or make_http(settings)
    sources = build_sources(select_sources(config["sources"], only), http)
    real_today = date.today()
    today = today or real_today
    # A run dated another day (tests, backfills) stamps "first seen" on that day too, so lead times
    # and staleness stay consistent.
    clock = None if today == real_today else (lambda: datetime.combine(today, datetime.now().time()).isoformat(timespec="seconds"))
    store = Store(settings.get("database", "data/agenda_watch.db"), now=clock)

    lookback = backfill_days if backfill_days is not None else int(settings.get("lookback_days", 10))
    start = today - timedelta(days=lookback)
    end = today + timedelta(days=int(settings.get("lookahead_days", 60)))
    # The report can show further back than we re-check: meetings already in the database stay
    # visible for report_lookback_days even though only lookback_days are fetched again.
    report_start = min(start, today - timedelta(days=int(settings.get("report_lookback_days", 30))))

    run_id = store.start_run()
    fingerprint = terms_fingerprint(config["terms"])
    if store.get_meta("terms_fingerprint") != fingerprint:
        docs, added, removed = rematch_stored(store, terms, settings, run_id, today)
        store.set_meta("terms_fingerprint", fingerprint)
        log.info("terms changed: re-matched %d stored agenda(s), %d match(es) added, %d removed", docs, added, removed,
                 extra={"fields": {"event": "rematched", "documents": docs, "added": added, "removed": removed}})
    statuses: list[SourceStatus] = []
    attempted = 0
    unexpected: list[Exception] = []
    try:
        for src in sources:
            st = SourceStatus(src.name, src.city, src.platform, section=src.section,
                              stale_after_days=src.stale_after_days, agenda_page=src.page_url())
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
                attempted += 1
                try:
                    _process(doc, store, http, terms, settings, run_id, st, today)
                except Exception as exc:  # noqa: BLE001 - one bad agenda shouldn't stop the others
                    unexpected.append(exc)
                    _contain(exc, doc, store, st)
        if attempted and len(unexpected) == attempted and len({type(e) for e in unexpected}) == 1:
            # Every agenda of every source failed the same way: almost certainly a bug, not bad data.
            first = unexpected[0]
            raise RuntimeError(f"all {attempted} agenda(s) failed with {type(first).__name__}: {first}") from first
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
        window = store.matches_in_window(report_start, end)
        # Per-city/section feed addresses: kept stable across runs (see feeds.py).
        feeds, retired = feed_paths.assign(feed_paths.watched_names(statuses, window), store.feed_history())
        store.record_feeds(feeds)
        return RunResult(
            run_id=run_id, start=start, end=end, statuses=statuses,
            new_matches=store.matches_for_run(run_id),
            upcoming=store.upcoming_with_matches(today),
            previous_run=store.previous_run_time(run_id),
            window_matches=window,
            archive_matches=store.matches_before(report_start),
            report_start=report_start,
            run_started=store.run_started(run_id),
            today=today,
            feeds=feeds,
            retired_feeds=retired,
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
