"""Normalization and fuzzy matching for OCR'd Hebrew. Paddle drops/merges letters on scans
("התחייבות" -> "התיבות" / "התבות" / "התחיבות"; "התקבל" -> "התקב"), so labels match by similarity."""
from __future__ import annotations

import re
from functools import lru_cache

_FINALS = str.maketrans({"ך": "כ", "ם": "מ", "ן": "נ", "ף": "פ", "ץ": "צ"})


@lru_cache(maxsize=200_000)
def norm_heb(s: str) -> str:
    """Letters/digits only, final letters folded, quotes/geresh removed."""
    s = s.translate(_FINALS)
    return re.sub(r"[^0-9A-Za-zא-ת]", "", s).lower()


def lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@lru_cache(maxsize=500_000)
def similarity(a: str, b: str) -> float:
    a, b = norm_heb(a), norm_heb(b)
    if not a or not b:
        return 0.0
    return 1 - lev(a, b) / max(len(a), len(b))


def lev_within(a: str, b: str, k: int) -> int | None:
    """Levenshtein distance if <= k, else None (banded DP with early exit)."""
    if abs(len(a) - len(b)) > k:
        return None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        lo, hi = max(1, i - k), min(len(b), i + k)
        if lo > 1:
            cur[lo - 1] = k + 1
        for j in range(lo, hi + 1):
            cur[j] = min(prev[j] + 1 if j <= i - 1 + k else k + 1, cur[j - 1] + 1, prev[j - 1] + (ca != b[j - 1]))
        for j in range(hi + 1, len(b) + 1):
            cur[j] = k + 1
        if min(cur[lo - 1:hi + 1]) > k:
            return None
        prev = cur
    return prev[-1] if prev[-1] <= k else None


def best_window_min(tokens_text: list[str], label: str, min_sim: float) -> tuple[float, int, int]:
    """Like best_window, but only windows that can reach `min_sim` are scored (bounded edit distance)."""
    target = norm_heb(label)
    if not target:
        return (0.0, -1, -1)
    best = (0.0, -1, -1)
    n_words = len(label.split())
    normed = [norm_heb(t) for t in tokens_text]
    for size in {max(1, n_words - 1), n_words, n_words + 1}:
        for i in range(len(normed) - size + 1):
            cand = "".join(normed[i:i + size])
            if not cand:
                continue
            k = int((1 - min_sim) * max(len(cand), len(target)) + 1e-9)   # float guard: 0.2*10 -> 2, not 1
            d = lev_within(cand, target, k)
            if d is not None:
                s = 1 - d / max(len(cand), len(target))
                if s > best[0]:
                    best = (s, i, i + size)
    return best


def best_window(tokens_text: list[str], label: str) -> tuple[float, int, int]:
    """Best similarity of `label` against any run of 1..3 consecutive tokens. (score, start, end_exclusive)."""
    best = (0.0, -1, -1)
    n_words = len(label.split())
    for size in {max(1, n_words - 1), n_words, n_words + 1}:
        for i in range(len(tokens_text) - size + 1):
            s = similarity("".join(tokens_text[i:i + size]), label)
            if s > best[0]:
                best = (s, i, i + size)
    return best


DIGITS = re.compile(r"\d+")


def digits_only(s: str) -> str:
    """All digits joined — for COMPARING values only. Never use it to build a value: '606/50364186'
    would become 60650364186, a number that is not on the page."""
    return "".join(DIGITS.findall(s))


def number_value(s: str, min_digits: int = 4) -> str | None:
    """The one contiguous digit group with >= min_digits. None if there is none or more than one (ambiguous)."""
    groups = [g for g in DIGITS.findall(s) if len(g) >= min_digits]
    return groups[0] if len(groups) == 1 else None


def letters_ratio(s: str) -> float:
    s = s.strip()
    return sum(ch.isalpha() for ch in s) / len(s) if s else 0.0


DATE_RE = re.compile(r"(\d{1,2})\s*[./\-]+\s*(\d{1,2})\s*[./\-]+\s*(\d{2,4})")


def parse_date(s: str) -> str | None:
    """DD/MM/YYYY-like text -> ISO date, only if it is a real calendar date (no repair)."""
    from datetime import date
    m = DATE_RE.search(s)
    if not m:
        return None
    day, month, y = int(m[1]), int(m[2]), m[3]
    if len(y) == 3:
        return None
    year = int(y) + (2000 if len(y) == 2 else 0)
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None
