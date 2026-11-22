"""SQLite store: which agendas were read, their text, and which matches were already reported."""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    url TEXT PRIMARY KEY,
    source TEXT, city TEXT, body TEXT, meeting_date TEXT, title TEXT, kind TEXT,
    detail_url TEXT, sha256 TEXT, pages INTEGER, chars INTEGER,
    status TEXT, note TEXT, first_seen TEXT, last_checked TEXT
);
CREATE TABLE IF NOT EXISTS pages (
    url TEXT, page INTEGER, text TEXT, PRIMARY KEY (url, page)
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE,
    url TEXT, term TEXT, page INTEGER,
    before TEXT, hit TEXT, after TEXT,
    run_id INTEGER, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, started TEXT, finished TEXT
);
CREATE TABLE IF NOT EXISTS source_runs (
    run_id INTEGER, source TEXT,
    listed INTEGER, read INTEGER, unchanged INTEGER, skipped INTEGER, failed INTEGER,
    new_matches INTEGER, error TEXT,
    PRIMARY KEY (run_id, source)
);
CREATE INDEX IF NOT EXISTS idx_matches_run ON matches(run_id);
CREATE INDEX IF NOT EXISTS idx_docs_date ON documents(meeting_date);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path, now=None):
        """`now` returns the current time as an ISO string; tests pass a clock for a simulated day."""
        self.now = now or _now
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # runs
    def start_run(self) -> int:
        cur = self.db.execute("INSERT INTO runs (started) VALUES (?)", (self.now(),))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int) -> None:
        self.db.execute("UPDATE runs SET finished=? WHERE id=?", (self.now(), run_id))
        self.db.commit()

    def previous_run_time(self, run_id: int) -> str | None:
        row = self.db.execute(
            "SELECT started FROM runs WHERE id < ? AND finished IS NOT NULL ORDER BY id DESC LIMIT 1", (run_id,)
        ).fetchone()
        return row["started"] if row else None

    # documents
    def get_document(self, url: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()

    def save_document(self, doc, sha: str | None, pages: list[str] | None, status: str, note: str = "") -> None:
        now = self.now()
        existing = self.get_document(doc.url)
        mdate = doc.meeting_date.isoformat() if doc.meeting_date else None
        n_pages = len(pages) if pages is not None else (existing["pages"] if existing else None)
        chars = sum(len(p) for p in pages) if pages is not None else (existing["chars"] if existing else None)
        self.db.execute(
            """INSERT INTO documents (url, source, city, body, meeting_date, title, kind, detail_url,
                   sha256, pages, chars, status, note, first_seen, last_checked)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                   source=excluded.source, city=excluded.city, body=excluded.body,
                   meeting_date=excluded.meeting_date, title=excluded.title, kind=excluded.kind,
                   detail_url=excluded.detail_url,
                   sha256=COALESCE(excluded.sha256, documents.sha256),
                   pages=excluded.pages, chars=excluded.chars,
                   status=excluded.status, note=excluded.note, last_checked=excluded.last_checked""",
            (doc.url, doc.source, doc.city, doc.body, mdate, doc.title, doc.kind, doc.detail_url,
             sha, n_pages, chars, status, note, now, now),
        )
        if pages is not None:
            self.db.execute("DELETE FROM pages WHERE url=?", (doc.url,))
            self.db.executemany(
                "INSERT INTO pages (url, page, text) VALUES (?,?,?)",
                [(doc.url, i, t) for i, t in enumerate(pages, start=1)],
            )
        self.db.commit()

    # matches
    def add_hits(self, url: str, hits, run_id: int, scope: str) -> int:
        """Insert hits; return how many were new. `scope` (city|date) keeps keys unique per meeting."""
        new = 0
        now = self.now()
        for h in hits:
            key = f"{scope}|{h.key}"
            cur = self.db.execute(
                """INSERT OR IGNORE INTO matches (key, url, term, page, before, hit, after, run_id, first_seen)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (key, url, h.term, h.page, h.before, h.hit, h.after, run_id, now),
            )
            new += cur.rowcount
        self.db.commit()
        return new

    def matches_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT m.*, d.source, d.city, d.body, d.meeting_date, d.title, d.kind, d.detail_url
               FROM matches m JOIN documents d ON d.url = m.url
               WHERE m.run_id = ? ORDER BY d.meeting_date, d.city, d.body, m.term, m.page""",
            (run_id,),
        ).fetchall()

    def matches_in_window(self, start: date, end: date) -> list[sqlite3.Row]:
        """Every match for meetings dated start..end (plus undated agendas first seen since start),
        no matter which run found it."""
        return self.db.execute(
            """SELECT m.*, d.source, d.city, d.body, d.meeting_date, d.title, d.kind, d.detail_url
               FROM matches m JOIN documents d ON d.url = m.url
               WHERE (d.meeting_date >= ? AND d.meeting_date <= ?)
                  OR (d.meeting_date IS NULL AND m.first_seen >= ?)
               ORDER BY d.meeting_date, d.city, d.body, m.term, m.page""",
            (start.isoformat(), end.isoformat(), start.isoformat()),
        ).fetchall()

    def prune_pages(self, before: date) -> int:
        """Drop stored page text for agendas of meetings before `before` (matches are kept).
        Returns how many agendas were pruned. Shrinks the file with VACUUM when anything went."""
        urls = [r["url"] for r in self.db.execute(
            """SELECT DISTINCT p.url FROM pages p JOIN documents d ON d.url = p.url
               WHERE d.meeting_date IS NOT NULL AND d.meeting_date < ?""", (before.isoformat(),))]
        if urls:
            self.db.executemany("DELETE FROM pages WHERE url=?", [(u,) for u in urls])
            self.db.commit()
            self.db.execute("VACUUM")
        return len(urls)

    # source health ---------------------------------------------------------
    def record_source_run(self, run_id: int, st) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO source_runs
               (run_id, source, listed, read, unchanged, skipped, failed, new_matches, error)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, st.name, st.listed, st.read, st.unchanged, st.skipped, st.failed, st.new_matches,
             st.error or None),
        )
        self.db.commit()

    def source_health(self, today: date, lead_window_days: int = 180) -> dict[str, dict]:
        """Per source: when it last produced a new agenda, how much notice its agendas give, and how
        long it has been failing.

        Lead time is measured per meeting (an agenda and a later packet count once) as meeting date
        minus the day the first document for it was seen. Documents found on the source's first day
        are left out: that's the initial backfill, not a sign of how early agendas get posted.
        """
        out: dict[str, dict] = {}
        for r in self.db.execute(
                "SELECT source, MIN(first_seen) AS first_ever, MAX(first_seen) AS last_new FROM documents GROUP BY source"):
            out[r["source"]] = {"first_ever": r["first_ever"][:10], "last_new": r["last_new"][:10],
                                "lead_days": None, "lead_samples": 0, "failing_runs": 0, "failing_since": None}
        since = (today - timedelta(days=lead_window_days)).isoformat()
        leads: dict[str, list[int]] = {}
        for r in self.db.execute(
                """SELECT source, body, meeting_date, MIN(first_seen) AS seen FROM documents
                   WHERE meeting_date IS NOT NULL AND first_seen >= ?
                   GROUP BY source, body, meeting_date""", (since,)):
            h = out.get(r["source"])
            if not h or r["seen"][:10] == h["first_ever"]:
                continue
            lead = (date.fromisoformat(r["meeting_date"]) - date.fromisoformat(r["seen"][:10])).days
            leads.setdefault(r["source"], []).append(lead)
        for name, values in leads.items():
            out[name]["lead_days"] = statistics.median(values)
            out[name]["lead_samples"] = len(values)
        # current failure streak, newest run first
        streak: dict[str, tuple[int, str | None]] = {}
        done: set[str] = set()
        for r in self.db.execute(
                """SELECT sr.source, sr.error, r.started FROM source_runs sr JOIN runs r ON r.id = sr.run_id
                   ORDER BY sr.run_id DESC"""):
            src = r["source"]
            if src in done:
                continue
            if r["error"]:
                n, _ = streak.get(src, (0, None))
                streak[src] = (n + 1, r["started"])
            else:
                done.add(src)
        for name, (n, started) in streak.items():
            h = out.setdefault(name, {"first_ever": None, "last_new": None, "lead_days": None,
                                      "lead_samples": 0, "failing_runs": 0, "failing_since": None})
            h["failing_runs"], h["failing_since"] = n, started
        return out

    def run_started(self, run_id: int) -> str | None:
        row = self.db.execute("SELECT started FROM runs WHERE id=?", (run_id,)).fetchone()
        return row["started"] if row else None

    def upcoming_with_matches(self, today: date) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT d.url, d.source, d.city, d.body, d.meeting_date, d.title, d.kind, d.detail_url,
                      GROUP_CONCAT(DISTINCT m.term) AS terms, COUNT(m.id) AS n
               FROM documents d JOIN matches m ON m.url = d.url
               WHERE d.meeting_date >= ? GROUP BY d.url ORDER BY d.meeting_date, d.city""",
            (today.isoformat(),),
        ).fetchall()

    def search(self, regex, city: str | None = None, limit: int = 200):
        q = "SELECT p.url, p.page, p.text, d.city, d.body, d.meeting_date FROM pages p JOIN documents d ON d.url=p.url"
        args: tuple = ()
        if city:
            q += " WHERE lower(d.city) LIKE ?"
            args = (f"%{city.lower()}%",)
        q += " ORDER BY d.meeting_date DESC"
        out = []
        for row in self.db.execute(q, args):
            for m in regex.finditer(row["text"] or ""):
                out.append((row, m))
                if len(out) >= limit:
                    return out
        return out
