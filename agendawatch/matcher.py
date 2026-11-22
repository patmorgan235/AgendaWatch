"""Key-term matching with word boundaries, case rules and false-positive filters."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class Term:
    name: str
    regex: re.Pattern
    exclude: list[re.Pattern] = field(default_factory=list)
    exclude_window: int = 150


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


def compile_terms(raw_terms: list) -> list[Term]:
    terms: list[Term] = []
    for raw in raw_terms:
        spec = {"name": raw} if isinstance(raw, str) else dict(raw)
        name = spec["name"]
        pattern = spec.get("pattern", name)
        body = pattern if spec.get("regex") else _phrase_to_regex(pattern)
        if spec.get("whole_word", True):
            body = rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])"
        flags = 0 if spec.get("case_sensitive") else re.IGNORECASE
        try:
            regex = re.compile(body, flags)
        except re.error as exc:
            raise ValueError(f"Term '{name}' has an invalid pattern: {exc}") from exc
        exclude = [re.compile(_phrase_to_regex(p), re.IGNORECASE) for p in spec.get("exclude_if_nearby", [])]
        terms.append(Term(name, regex, exclude, int(spec.get("exclude_window", 150))))
    return terms


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def find_hits(pages: list[str], terms: list[Term], context: int = 140, max_per_term: int = 5) -> tuple[list[Hit], dict[str, int]]:
    """Return (snippets, total match count per term) for one document."""
    hits: list[Hit] = []
    counts: dict[str, int] = {}
    for term in terms:
        kept = 0
        seen_keys: set[str] = set()
        for page_no, text in enumerate(pages, start=1):
            if not text:
                continue
            for m in term.regex.finditer(text):
                if term.exclude:
                    w = term.exclude_window
                    window = text[max(0, m.start() - w): m.end() + w]
                    if any(ex.search(window) for ex in term.exclude):
                        continue
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
