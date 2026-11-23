"""Stable addresses for the per-city and per-section feeds (feeds/city/<slug>, feeds/section/<slug>).

A calendar subscription silently keeps whatever its URL serves, so an address must never start
serving a different city, and an address that stops being updated must say so. The rules:

- A city or section keeps the path it was first published at. Paths are recorded in the
  database (Store.feed_history), so this holds across runs and config edits.
- A new name gets feeds/<kind>/<slug>. If that path was ever used by another name, or another
  new name in the same run has the same slug ("St Paul" / "St. Paul"), it gets
  <slug>-<6 hex digits of a hash of the name> instead. That doesn't depend on config order.
- A path whose name is no longer watched is "retired": the site writes an empty feed there
  that says it is no longer updated, instead of leaving its last events up forever.

Names come from the configured sources and from the meetings in the report window. So a
meeting from a source that was removed or renamed still has a city and section feed while it
stays on the page.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

OTHER = "Other sources"   # the report section for sources not in the config (site.OTHER)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "feed"


def _hashed(name: str) -> str:
    return f"{slug(name)}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:6]}"


def watched_names(statuses, window_rows) -> list[tuple[str, str]]:
    """(kind, name) pairs to publish feeds for: config order first, then names only seen in
    the report window's meetings (sources since removed or renamed)."""
    section_of = {s.name: s.section or OTHER for s in statuses}
    names = [("section", s.section or OTHER) for s in statuses] + [("city", s.city) for s in statuses]
    for r in window_rows:
        names += [("section", section_of.get(r["source"], OTHER)), ("city", r["city"])]
    return [n for n in dict.fromkeys(names) if n[1]]


def assign(names: list[tuple[str, str]], history: list[dict]) -> tuple[list[dict], list[dict]]:
    """Paths for `names`, given every feed published before (dicts with kind, name, path).

    Returns (current, retired): current has one {kind, name, path} per name; retired lists
    the earlier feeds whose name isn't watched any more."""
    known = {(h["kind"], h["name"]): h["path"] for h in history}
    used = set(known.values())
    new_slugs = Counter((kind, slug(name)) for kind, name in names if (kind, name) not in known)
    current = []
    for kind, name in names:
        path = known.get((kind, name))
        if path is None:
            plain = f"feeds/{kind}/{slug(name)}"
            unique = plain not in used and new_slugs[(kind, slug(name))] == 1
            path = plain if unique else f"feeds/{kind}/{_hashed(name)}"
        current.append({"kind": kind, "name": name, "path": path})
    watched = set(names)
    retired = [h for h in history if (h["kind"], h["name"]) not in watched]
    return current, retired
