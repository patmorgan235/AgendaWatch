"""Find a meeting date in free text, table rows or URLs."""

from __future__ import annotations

import re
from datetime import date

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}

_PATTERNS = [
    # 10/27/2026
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "mdy"),
    # 2026-10-27 (also ISO timestamps)
    (re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})"), "ymd"),
    # August 3-4, 2026 / Aug 3 & 4, 2026 (a multi-day meeting: use the first day)
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*(?:-|–|&|and|to)\s*\d{1,2},?\s+(\d{4})\b"), "Mdy"),
    # Oct 26, 2026 / October 26 2026 / Sept. 8, 2026
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b"), "Mdy"),
    # 11/05/26
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b"), "mdy2"),
    # 20260414 inside a URL or file name
    (re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)"), "ymd"),
    # 11-05-2026 / 1-23-2025 inside a file name
    (re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})-(20\d{2})(?!\d)"), "mdy"),
    # 11052026 (MMDDYYYY) inside a file name
    (re.compile(r"(?<!\d)(0[1-9]|1[0-2])([0-2]\d|3[01])(20\d{2})(?!\d)"), "mdy"),
]


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    for regex, kind in _PATTERNS:
        for m in regex.finditer(text):
            a, b, c = m.groups()
            try:
                if kind == "mdy":
                    return date(int(c), int(a), int(b))
                if kind == "mdy2":
                    return date(2000 + int(c), int(a), int(b))
                if kind == "ymd":
                    return date(int(a), int(b), int(c))
                if kind == "Mdy":
                    month = _MONTHS.get(a.lower())
                    if month:
                        return date(int(c), month, int(b))
            except ValueError:
                continue
    return None
