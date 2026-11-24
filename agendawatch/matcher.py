"""Key-term matching with word boundaries, case rules and false-positive filters."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# Bump when matching itself changes (not just the term list), so stored agendas are re-matched:
# 2 = longest match wins across terms; exclude_context.
# 3 = exclude_if_nearby ignores spacing.
MATCHER_VERSION = 3


@dataclass
class Term:
    name: str
    regex: re.Pattern
    exclude: list[re.Pattern] = field(default_factory=list)
    exclude_window: int = 150
    # Patterns that rule out a match only when they cover the match itself, e.g. "Ford Transit"
    # for the term "Transit". Narrower than exclude_if_nearby, which looks ~150 characters around.
    exclude_context: list[re.Pattern] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)   # "group:value", e.g. "agency:DART", "mode:rail"
    # A match counts only when one of these is within exclude_window (e.g. GMP needs "DART" nearby).
    require: list[re.Pattern] = field(default_factory=list)


@dataclass
class Hit:
    term: str
    page: int
    before: str
    hit: str
    after: str
    anchor: str = ""  # the line the match sits on

    @property
    def key(self) -> str:
        """Stable fingerprint so the same passage isn't reported twice.

        Uses the line containing the match rather than the wider snippet, so an amended
        agenda that adds text nearby doesn't make an old item look new.
        """
        basis = self.anchor or f"{self.before}{self.hit}{self.after}"
        norm = re.sub(r"\s+", " ", basis).strip().lower()
        return hashlib.sha1(f"{self.term}|{norm}".encode("utf-8")).hexdigest()


def _phrase_to_regex(phrase: str) -> str:
    # Any run of whitespace in the phrase matches any whitespace in the document,
    # because PDF text often breaks lines in the middle of a phrase.
    return r"\s+".join(re.escape(part) for part in phrase.split())


def _spelling(spec: dict) -> str:
    """One spelling of a term as a regex with its own case rule, so spellings can be combined."""
    pattern = spec.get("pattern", spec.get("name"))
    body = pattern if spec.get("regex") else _phrase_to_regex(pattern)
    if spec.get("whole_word", True):
        body = rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])"
    return ("(?-i:" if spec.get("case_sensitive") else "(?i:") + body + ")"


def compile_terms(raw_terms: list) -> list[Term]:
    terms: list[Term] = []
    for raw in raw_terms:
        spec = {"name": raw} if isinstance(raw, str) else dict(raw)
        name = spec["name"]
        # aliases: other spellings reported under this one term, e.g. "Dallas Area Rapid Transit" for DART.
        # A plain string is a phrase (ignores case); a mapping takes pattern/regex/case_sensitive/whole_word.
        spellings = [_spelling(spec)] + [
            _spelling({"pattern": a} if isinstance(a, str) else a) for a in spec.get("aliases", [])]
        try:
            regex = re.compile("|".join(spellings))
        except re.error as exc:
            raise ValueError(f"Term '{name}' has an invalid pattern: {exc}") from exc
        # Spacing is ignored: PDF text often loses or adds spaces ("GuaranteedMaximumPrice", "CM AR").
        def spaced(phrase):
            return re.compile(r"\s*".join(re.escape(ch) for ch in re.sub(r"\s+", "", phrase)), re.IGNORECASE)
        exclude = [spaced(p) for p in spec.get("exclude_if_nearby", [])]
        require = [spaced(p) for p in spec.get("require_nearby", [])]
        try:
            context = [re.compile(p, re.IGNORECASE) for p in spec.get("exclude_context", [])]
        except re.error as exc:
            raise ValueError(f"Term '{name}' has an invalid exclude_context pattern: {exc}") from exc
        tags = [str(t) for t in spec.get("tags", [])]
        bad = [t for t in tags if ":" not in t]
        if bad:
            raise ValueError(f"Term '{name}': tags must look like group:value (got {bad})")
        terms.append(Term(name, regex, exclude, int(spec.get("exclude_window", 150)), context, tags, require))
    return terms


def term_tags(raw_terms: list) -> dict[str, list[str]]:
    """{term name: tags} straight from the config (used when building the report)."""
    return {(t if isinstance(t, str) else t["name"]): ([] if isinstance(t, str) else list(t.get("tags", [])))
            for t in raw_terms}


CONTEXT_REACH = 40   # how far an exclude_context pattern may extend beyond the match


def _excluded(term: Term, text: str, m: re.Match) -> bool:
    if term.exclude or term.require:
        w = term.exclude_window
        window = text[max(0, m.start() - w): m.end() + w]
        if any(ex.search(window) for ex in term.exclude):
            return True
        if term.require and not any(rq.search(window) for rq in term.require):
            return True
    if term.exclude_context:
        lo = max(0, m.start() - CONTEXT_REACH)
        window = text[lo: m.end() + CONTEXT_REACH]
        for ex in term.exclude_context:
            for c in ex.finditer(window):
                if c.start() + lo <= m.start() and c.end() + lo >= m.end():
                    return True
    return False


def _drop_overlaps(found: list[tuple[Term, re.Match]]) -> list[tuple[Term, re.Match]]:
    """Keep the longest match where matches of different terms overlap, so "Transit" inside
    "Dallas Area Rapid Transit" or "transit-oriented development" counts only as the longer term."""
    keep = []
    for term, m in found:
        inside_longer = any(
            other is not term and om.start() <= m.start() and om.end() >= m.end()
            and (om.end() - om.start()) > (m.end() - m.start())
            for other, om in found)
        if not inside_longer:
            keep.append((term, m))
    return keep


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def find_hits(pages: list[str], terms: list[Term], context: int = 140, max_per_term: int = 5) -> tuple[list[Hit], dict[str, int]]:
    """Return (snippets, total match count per term) for one document."""
    # Pass 1: every term's matches per page, minus exclusions and matches inside a longer term's.
    by_term: dict[str, list[tuple[int, str, re.Match]]] = {t.name: [] for t in terms}
    for page_no, text in enumerate(pages, start=1):
        if not text:
            continue
        found = [(t, m) for t in terms for m in t.regex.finditer(text) if not _excluded(t, text, m)]
        for t, m in _drop_overlaps(found):
            by_term[t.name].append((page_no, text, m))
    # Pass 2: snippets, in term order then page order, up to max_per_term distinct passages each.
    hits: list[Hit] = []
    counts: dict[str, int] = {}
    for term in terms:
        kept = 0
        seen_keys: set[str] = set()
        for page_no, text, m in sorted(by_term[term.name], key=lambda x: (x[0], x[2].start())):
            counts[term.name] = counts.get(term.name, 0) + 1
            if kept >= max_per_term:
                continue
            start = max(0, m.start() - context)
            end = min(len(text), m.end() + context)
            before = _clean(text[start:m.start()]).lstrip()
            after = _clean(text[m.end():end]).rstrip()
            if start > 0:
                before = "…" + before
            if end < len(text):
                after = after + "…"
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            anchor = text[line_start: line_end if line_end != -1 else len(text)]
            h = Hit(term.name, page_no, before, _clean(m.group(0)), after, anchor)
            if h.key in seen_keys:
                continue
            seen_keys.add(h.key)
            hits.append(h)
            kept += 1
    return hits, counts
