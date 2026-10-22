"""Closed list of sending institutions — the APPROVED list data/Institutions.xlsx (21.10; replaces the 49-name list).
Index: data/institutions_v2.json (one record per Excel row; built by tools/build_institutions.py).
Search keys: curated aliases (data/institution_aliases_v2.json) + automatic full-name keys (unique, >= 2 words,
>= 10 letters). A key shared by several records is ambiguous. Being on the list is NOT evidence of being the sender.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .textmatch import best_window_min, norm_heb, similarity

DATA = Path(__file__).resolve().parent.parent / "data"
_IDX = json.loads((DATA / "institutions_v2.json").read_text(encoding="utf-8"))
_AL = json.loads((DATA / "institution_aliases_v2.json").read_text(encoding="utf-8"))
RECORDS: dict[str, dict] = {r["rec_id"]: r for r in _IDX["records"]}
assert len(RECORDS) == len(_IDX["records"]), "rec_id not unique"
INSTITUTION_WORDS = [norm_heb(w) for w in _AL["institution_words"]]

# entries: (alias_norm, alias_text, rec_ids, kind) ; kind = curated | full_name
ENTRIES: list[tuple[str, str, tuple[str, ...], str]] = []
RECEIVER_RECS, CONTEXT_ALIASES = set(), set()
for e in _AL["aliases"]:
    for rec in e["rec"]:
        assert rec in RECORDS, f"alias target not on the approved list: {rec}"
    for a in e["aliases"]:
        ENTRIES.append((norm_heb(a), a, tuple(e["rec"]), "curated"))
        if e.get("needs_context"):
            CONTEXT_ALIASES.add(norm_heb(a))
    if e.get("receiver_side"):
        RECEIVER_RECS.update(e["rec"])
DISTINCTIVE: dict[str, list[str]] = {}          # full-name key -> its distinctive words (must match in fuzzy hits)
for key, spec in _IDX["auto_keys"].items():
    ENTRIES.append((key, RECORDS[spec["rec"]]["name"], (spec["rec"],), "full_name"))
    DISTINCTIVE[key] = spec["distinctive"]

_alias_owners = defaultdict(set)
for an, _, recs, _ in ENTRIES:
    _alias_owners[an].update(recs)
AMBIGUOUS = {an for an, recs in _alias_owners.items() if len(recs) > 1}

# names used as candidate values: the record's official name (registration number kept alongside)
def display(rec_id: str) -> str:
    return RECORDS[rec_id]["name"]


RECEIVER_SIDE = {display(r) for r in RECEIVER_RECS}
SENDERS = defaultdict(list)                      # display name -> aliases (compat for guards / eval)
for an, a, recs, _ in ENTRIES:
    for r in recs:
        SENDERS[display(r)].append(a)
SENDERS = dict(SENDERS)
NEEDS_CONTEXT, OBSERVED, AMBIGUOUS_ALIASES = set(), set(), AMBIGUOUS   # compat names (OBSERVED: none — all listed)


def has_institution_word(row_text_norm: str) -> bool:
    return any(w and w in row_text_norm for w in INSTITUTION_WORDS)


def _trigrams(s: str) -> set[str]:
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))}


def match_row(row, min_sim: float = 0.8) -> list[dict]:
    """All list matches in one OCR row: [{recs, alias, sim, tokens, kind}], longest match wins on shared tokens."""
    texts = [t.text for t in row]
    row_norm = norm_heb("".join(texts))
    row_tri = _trigrams(row_norm)
    context = has_institution_word(row_norm)
    hits = []
    for an, a, recs, kind in ENTRIES:
        if not an:
            continue
        if an in CONTEXT_ALIASES and not context:
            continue                                   # 'מאיר', 'כרמל', 'רפאל' only next to מרכז / בית חולים
        if len(an) <= 4:
            toks = [t for t in row if norm_heb(t.text) == an]
            s = 1.0 if toks else 0.0
        else:
            if kind == "full_name" and an not in row_norm:
                tri = _trigrams(an)
                if not tri or len(tri & row_tri) / len(tri) < 0.7:
                    continue                           # cheap prefilter before fuzzy matching a long name
            s, i, j = best_window_min(texts, a, min_sim)   # bounded: only windows that can reach min_sim
            toks = row[i:j] if s >= min_sim else []
            if toks and kind == "full_name" and an not in row_norm:
                # fuzzy full-name hit: its distinctive word(s) must be read too ('שהם' is not 'שמיר')
                win = [norm_heb(t.text) for t in toks]
                if not all(any(similarity(w, d) >= 0.8 for w in win) for d in DISTINCTIVE.get(an, [])):
                    toks = []
            if an in row_norm:
                s = max(s, 1.0 if toks else s)
        if toks:
            hits.append({"recs": recs, "alias": a, "alias_norm": an, "sim": round(s, 2),
                         "tokens": toks, "kind": kind, "ambiguous": an in AMBIGUOUS})
    # longest match wins: drop a hit whose tokens are all inside a longer hit for a different record set
    keep = []
    for h in hits:
        ids = {t.id for t in h["tokens"]}
        inside = any(o is not h and len(o["alias_norm"]) > len(h["alias_norm"]) and ids <= {t.id for t in o["tokens"]}
                     and set(o["recs"]) != set(h["recs"]) for o in hits)
        if not inside:
            keep.append(h)
    return keep


def resolve_text(text: str) -> set[str]:
    """Display names whose alias occurs in `text` (truth mapping; longest alias only)."""
    n = norm_heb(text or "")
    found = [(len(an), recs) for an, _, recs, _ in ENTRIES if len(an) >= 3 and an in n]
    if not found:
        return set()
    best = max(l for l, _ in found)
    return {display(r) for l, recs in found if l == best for r in recs}
