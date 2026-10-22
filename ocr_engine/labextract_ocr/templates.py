"""Form families + deterministic strategy rules (plan section 13, approach F).

The ROLE of a value comes from the form family's layout, decided in code — never from a model. A family is
recognized by anchors read on the page; several families may match; an unknown page falls back to the generic
label rules in fields.py. Each rule returns candidates with `rule` and `family` for traceability.

Families seen in the 26 part-1 samples (anchors are layout words, not values):
  insurance_table   HMO system screenshot: table header "מבטח … מספר … סוג כיסוי"; commitment = column "מספר"
  commitment_title  printed commitment form: title "התחייבות לתשלום עבור שירותי בריאות"; "מספר N" on the next rows
  lab_report        lab results report: "דוח תוצאות … מס N"
  idf_budget        IDF referral: "אסמכתא (תקציבית) (מס/מספר) N"; NOT "מספר הפניה" (referral number)
  rosenblatt        Rosenblatt lab: "אסמכתא" holds the CENTRAL number -> no commitment rule
"""
from __future__ import annotations

from .layout import h_overlap
from .ocr import Token
from .textmatch import norm_heb, number_value, similarity


def _sim_any(t: Token, words, th=0.75) -> bool:
    """Fuzzy match, or containment for tokens Paddle merged ('סוגכיסו' = 'סוג' + 'כיסוי')."""
    n = norm_heb(t.text)
    return any(similarity(t.text, w) >= th or (len(norm_heb(w)) >= 3 and norm_heb(w) in n) for w in words)


def _row_has(row, *groups, th=0.75) -> bool:
    """Every group (a tuple of alternatives) is matched by some token of the row."""
    return all(any(_sim_any(t, g, th) for t in row) for g in groups)


def classify(rows: list[list[Token]]) -> dict[str, list[int]]:
    """family -> indices of its anchor rows."""
    fam: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        if _row_has(row, ("מבטח",), ("סוג", "כיסוי", "כיסו")):
            fam.setdefault("insurance_table", []).append(i)
        if _row_has(row, ("לתשלום",), ("שירותי", "בריאות", "כריאות")):
            fam.setdefault("commitment_title", []).append(i)
        if _row_has(row, ("דוח", "דח"), ("תוצאות",)):
            fam.setdefault("lab_report", []).append(i)
        if _row_has(row, ("אסמכתא",), ("תקציבית", "מס", "מספר"), th=0.8):
            fam.setdefault("idf_budget", []).append(i)
        if any(similarity(t.text, "רוזנבלט") >= 0.8 for t in row):
            fam.setdefault("rosenblatt", []).append(i)
    return fam


def _numbers_left_of(row, anchor: Token, min_digits=6, max_gap=420):
    out = []
    for t in sorted(row, key=lambda x: -x.cx):
        if t.cx < anchor.bbox[0] and anchor.bbox[0] - t.bbox[2] <= max_gap:
            v = number_value(t.text, min_digits)
            if v and not any(c in t.text for c in "/-:"):
                out.append((t, v))
    return out


def _cand(t: Token, v: str, rule: str, family: str) -> dict:
    return {"value": v, "raw": t.text, "conf": t.conf, "bbox": t.bbox, "token_id": t.id,
            "evidence": [f"family {family}: {rule}"], "rule": rule, "family": family, "role": "strong"}


def commitment_rules(fam: dict, rows) -> tuple[list[dict], list[str]]:
    """(candidates, notes). notes may contain 'count_uncertain'."""
    cands, notes = [], []
    if "insurance_table" in fam:
        for hi in fam["insurance_table"]:
            header = rows[hi]
            col = next((t for t in header if similarity(t.text, "מספר") >= 0.6), None)
            if not col:
                continue
            found, col_boxes = 0, []
            for row in rows[hi + 1:hi + 7]:
                in_col = [t for t in row if h_overlap(t.bbox, col.bbox) > 0]
                nums = [(t, number_value(t.text, 6)) for t in in_col if h_overlap(t.bbox, col.bbox) >= 0.5]
                nums = [(t, v) for t, v in nums if v and not any(c in t.text for c in "/-:.")]
                if nums:
                    for t, v in nums[:1]:
                        cands.append(_cand(t, v, "insurance table, column 'מספר'", "insurance_table"))
                        col_boxes.append(t.bbox)
                    found += 1
                elif in_col and found and any(any(ch.isdigit() for ch in t.text) for t in in_col):
                    notes.append("count_uncertain")      # a further table row with an unreadable number
                    # the cell to re-read: the column's x-extent (from the numbers read in it) x this row's
                    # digit tokens inside that extent (a split / partly-outside token is included)
                    cx0, cx1 = min(b[0] for b in col_boxes), max(b[2] for b in col_boxes)
                    cell = [t for t in row if any(ch.isdigit() for ch in t.text)
                            and h_overlap(t.bbox, [cx0, 0, cx1, 0]) >= 0.5]
                    if cell:
                        notes.append({"unread_cell": [cx0, min(t.bbox[1] for t in cell), cx1,
                                                      max(t.bbox[3] for t in cell)],
                                      "tokens": [t.text for t in cell], "token_ids": [t.id for t in cell],
                                      "column_lengths": sorted({len(c["value"]) for c in cands
                                                                if c["family"] == "insurance_table"})})
                elif found:
                    break
    if "commitment_title" in fam:
        for ti in fam["commitment_title"]:
            for row in rows[ti:ti + 3]:
                label = next((t for t in row if similarity(t.text, "מספר") >= 0.6), None)
                if label:
                    for t, v in _numbers_left_of(row, label)[:1]:
                        cands.append(_cand(t, v, "title 'התחייבות לתשלום' + 'מספר N'", "commitment_title"))
    if "lab_report" in fam:
        for ai in fam["lab_report"]:
            label = next((t for t in rows[ai] if norm_heb(t.text) == "מס"), None)
            if label:
                for t, v in _numbers_left_of(rows[ai], label)[:1]:
                    cands.append(_cand(t, v, "'דוח תוצאות … מס N'", "lab_report"))
    if "idf_budget" in fam:
        for ai in fam["idf_budget"]:
            label = next((t for t in rows[ai] if similarity(t.text, "אסמכתא") >= 0.8), None)
            if label:
                for t, v in _numbers_left_of(rows[ai], label)[:1]:
                    cands.append(_cand(t, v, "'אסמכתא (תקציבית) מספר N'", "idf_budget"))
    return cands, notes


def generic_commitment_allowed(fam: dict) -> bool:
    """Generic label rules are used unless the family defines the commitment layout itself."""
    return not ({"insurance_table", "commitment_title", "lab_report", "idf_budget", "rosenblatt"} & set(fam))
