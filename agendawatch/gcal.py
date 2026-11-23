"""Push meetings into Google Calendars you own: one per city, per section, or one for everything.

Anyone can already subscribe to the published .ics feeds (feeds/city/<city>.ics); this is for
calendars you run yourself (for example an official "Plano transit agenda items" calendar),
which update right after each run and can be shared, made public or embedded.

Configure in config.yaml. A meeting goes to every calendar whose key is its city, its report
section, or "*":

    google_calendar:
      calendars:
        Plano: abc123@group.calendar.google.com
        "DART member cities": def456@group.calendar.google.com
        "*": all789@group.calendar.google.com

Credentials: a Google service account, from AGENDAWATCH_GOOGLE_CREDENTIALS (the key JSON itself)
or GOOGLE_APPLICATION_CREDENTIALS (a key file). Share each calendar with the service account's
email address with "Make changes to events". See docs/runbook.md, "Google Calendar sync".

Sync is idempotent: each meeting's event has a fixed ID ("ag" + meeting id) and a content hash,
so a run only inserts new meetings, updates changed ones and removes ones no longer listed.
Only events this tool created (marked with a private property) are ever touched, and events
before the report window are left alone as history.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import quote

from .site import event_text

log = logging.getLogger("agendawatch.gcal")

API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
MARKER = "agendawatch"            # private extended property on every event we create
RETRY_STATUSES = (429, 500, 502, 503, 504)
RATE_LIMIT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")


class GcalError(RuntimeError):
    pass


@dataclass
class SyncResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)   # one line per change (what --dry-run prints)

    def summary(self) -> str:
        return (f"{self.inserted} added, {self.updated} updated, {self.deleted} removed, "
                f"{self.unchanged} unchanged" + (f", {len(self.errors)} calendar(s) failed" if self.errors else ""))


# ---------------------------------------------------------------- configuration
def calendars_from_config(cfg: dict) -> dict[str, str]:
    """{city, section or "*": calendar id}"""
    gc = cfg.get("google_calendar") or {}
    return {str(k): str(v) for k, v in (gc.get("calendars") or {}).items() if v}


def session_from_env(env: dict | None = None):
    """An authorized requests session, or None when no credentials are set."""
    env = os.environ if env is None else env
    raw, path = env.get("AGENDAWATCH_GOOGLE_CREDENTIALS"), env.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw and not path:
        return None
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:
        raise GcalError('Google Calendar sync needs google-auth: pip install "google-auth>=2.29"') from exc
    try:
        if raw:
            creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
        else:
            creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    except (ValueError, OSError) as exc:
        raise GcalError(f"couldn't load the Google service account key: {exc}") from exc
    return AuthorizedSession(creds)


# ---------------------------------------------------------------- events
def event_id(m: dict) -> str:
    # Google event IDs allow only base32hex (letters a–v, digits), 5–1024 characters, so the prefix
    # must avoid w–z. A meeting id is 12 hex digits.
    return "ag" + m["id"]


def event_body(m: dict, base: str | None) -> dict:
    summary, desc, url = event_text(m, base)
    d = date.fromisoformat(m["date"])
    body = {
        "id": event_id(m),
        "summary": summary,
        "description": desc,
        "start": {"date": d.isoformat()},
        "end": {"date": (d + timedelta(days=1)).isoformat()},   # all-day: the end date is exclusive
        "transparency": "transparent",                           # don't block people's time
    }
    if url.startswith("http"):
        body["source"] = {"title": "Agenda Watch", "url": url}
    digest = hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    body["extendedProperties"] = {"private": {MARKER: "1", "hash": digest}}
    return body


def _hash(event: dict) -> str | None:
    return ((event.get("extendedProperties") or {}).get("private") or {}).get("hash")


def desired_events(data: dict, calendars: dict[str, str]) -> dict[str, dict[str, dict]]:
    """{calendar id: {event id: event body}} for every dated meeting in the report window."""
    out: dict[str, dict[str, dict]] = {cal: {} for cal in dict.fromkeys(calendars.values())}
    for m in data["meetings"]:
        if not m.get("date"):
            continue
        targets = {cal for key, cal in calendars.items() if key in ("*", m["city"], m["section"])}
        if not targets:
            continue
        body = event_body(m, data.get("site_url"))
        for cal in targets:
            out[cal][body["id"]] = body
    return out


# ---------------------------------------------------------------- API calls
def _rate_limited(resp) -> bool:
    try:
        errors = resp.json().get("error", {}).get("errors", [])
    except ValueError:
        return False
    return any(e.get("reason") in RATE_LIMIT_REASONS for e in errors)


def _call(session, method: str, url: str, sleep=time.sleep, tries: int = 4, **kw):
    for attempt in range(tries):
        resp = session.request(method, url, timeout=30, **kw)
        retry = resp.status_code in RETRY_STATUSES or (resp.status_code == 403 and _rate_limited(resp))
        if not retry or attempt == tries - 1:
            return resp
        sleep(2 ** attempt)
    return resp  # pragma: no cover - the loop always returns


def _check(resp, what: str) -> None:
    if resp.status_code < 400:
        return
    try:
        msg = resp.json().get("error", {}).get("message", "")
    except ValueError:
        msg = (resp.text or "")[:200]
    hint = ""
    if resp.status_code in (403, 404):
        hint = " (is the calendar shared with the service account, with 'Make changes to events'?)"
    raise GcalError(f"{what}: HTTP {resp.status_code} {msg}{hint}")


def _events_url(cal: str) -> str:
    return f"{API}/calendars/{quote(cal, safe='')}/events"


def _existing(session, cal: str, time_min: str, sleep) -> dict[str, dict]:
    """Events this tool created in `cal` that end after time_min, including deleted ones."""
    found: dict[str, dict] = {}
    token = None
    while True:
        params = {"privateExtendedProperty": f"{MARKER}=1", "timeMin": f"{time_min}T00:00:00Z",
                  "showDeleted": "true", "maxResults": "2500"}
        if token:
            params["pageToken"] = token
        resp = _call(session, "GET", _events_url(cal), sleep=sleep, params=params)
        _check(resp, f"listing events in {cal}")
        page = resp.json()
        for ev in page.get("items", []):
            found[ev["id"]] = ev
        token = page.get("nextPageToken")
        if not token:
            return found


def _start(event: dict) -> str:
    start = event.get("start") or {}
    return start.get("date") or (start.get("dateTime") or "")[:10]


# ---------------------------------------------------------------- sync
def sync(session, data: dict, calendars: dict[str, str], dry_run: bool = False, sleep=time.sleep) -> SyncResult:
    res = SyncResult()
    window_start = data["window"]["start"]
    for cal, wanted in desired_events(data, calendars).items():
        try:
            existing = _existing(session, cal, window_start, sleep)
            url = _events_url(cal)
            for eid, body in wanted.items():
                cur = existing.get(eid)
                if cur is None:
                    action = "add"
                elif cur.get("status") == "cancelled" or _hash(cur) != _hash(body):
                    action = "update"
                else:
                    res.unchanged += 1
                    continue
                res.actions.append(f"{action:<6} {cal}  {body['start']['date']}  {body['summary']}")
                if not dry_run:
                    if action == "add":
                        resp = _call(session, "POST", url, sleep=sleep, json=body)
                        if resp.status_code == 409:   # the id exists (deleted long ago): reuse it
                            resp = _call(session, "PUT", f"{url}/{eid}", sleep=sleep, json={**body, "status": "confirmed"})
                    else:
                        resp = _call(session, "PUT", f"{url}/{eid}", sleep=sleep, json={**body, "status": "confirmed"})
                    _check(resp, f"saving {body['summary']} in {cal}")
                if action == "add":
                    res.inserted += 1
                else:
                    res.updated += 1
            for eid, cur in existing.items():
                # Only our events, inside the window, that are no longer listed (e.g. a meeting renamed).
                if eid in wanted or cur.get("status") == "cancelled" or _start(cur) < window_start:
                    continue
                res.actions.append(f"remove {cal}  {_start(cur)}  {cur.get('summary', eid)}")
                if not dry_run:
                    resp = _call(session, "DELETE", f"{url}/{eid}", sleep=sleep)
                    if resp.status_code not in (404, 410):
                        _check(resp, f"removing {eid} from {cal}")
                res.deleted += 1
        except Exception as exc:  # noqa: BLE001 - one calendar's problem shouldn't stop the others
            res.errors.append(f"{cal}: {exc}")
            log.warning("google calendar %s: %s", cal, exc)
    return res


def sync_from_config(cfg: dict, data: dict, session=None, env: dict | None = None,
                     dry_run: bool = False) -> SyncResult | None:
    """Sync if calendars are configured and credentials are available; otherwise None."""
    calendars = calendars_from_config(cfg)
    if not calendars:
        return None
    session = session or session_from_env(env)
    if session is None:
        log.info("google calendar: %d calendar(s) configured but no credentials set; skipped", len(calendars))
        return None
    return sync(session, data, calendars, dry_run=dry_run)
