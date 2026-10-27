"""Measure the full-page path against the crops-first path on part 1 or part 2, every field, against the hand-filled
answers, with the automatic approval as the site runs it (backend/readiness.apply_auto_approval, switch on).

python tools/measure_privacy.py [--part 1|2] [--modes full_page,crops_first] [--pages 1,2,3] [--out out_measure]

Runs the real pipeline (engine_select.extract_page: local OCR + labx overlay), so it CALLS GEMINI. Model answers are
cached by labx, so an interrupted run continues where it stopped. Per mode it reports, per field: accepted correct /
accepted WRONG / not accepted, how many pages were sent as crops only vs the full page, and real model calls.
Names are never accepted; they are reported as proposals only. Patient values are not printed: only counts and
per-page marks (✓ correct, ✗ WRONG, · not accepted).
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(Path(os.environ.get("LABEXTRACT_OCR_DIR") or ROOT / "ocr_engine")))
sys.stdout.reconfigure(encoding="utf-8")

PARTS = {
    1: (Path(r"C:\accthon_chani\חומרים\התחייבויות - דוגמאות, חלק 1 (PDF).pdf"),
        Path(r"C:\accthon_chani\אקסל במילו ידני\מילוי ידני של דוגמאות נספח 1.xlsx")),
    2: (Path(r"C:\accthon_chani\חומרים\התחייבויות - דוגמאות, חלק 2 (PDF).pdf"),
        Path(r"C:\Users\ישראלי\Downloads\truth_part2.xlsx")),
}
COMPLETE = "COMPLETE"
APPROVED = "AUTO_APPROVED"


def _split(v):
    """'939158, 1616126' / '947468;947469' -> ['939158', '1616126']; 'אין' / empty -> []."""
    if v in (None, ""):
        return []
    return [x.strip().lstrip("0") for x in str(v).replace(";", ",").split(",") if x.strip() and x.strip() != "אין"]


def truth(part):
    import openpyxl
    import re
    rows = [r for r in openpyxl.load_workbook(PARTS[part][1]).worksheets[0].iter_rows(min_row=2, values_only=True)
            if r[0]]
    if part == 1:   # first, last, ID, central, date, sender, commitment - row i = page i
        return {n: {"id": str(r[2]).strip().lstrip("0"), "central": _split(r[3]),
                    "date": r[4].date().isoformat() if r[4] else None, "sender": str(r[5] or "").strip(),
                    "commitment": _split(r[6])} for n, r in enumerate(rows, 1)}
    out = {}        # file + page, sender, first, last, ID, central, date, commitment - a page may have several rows
    for r in rows:
        page = int(re.search(r"(\d+)\s*$", str(r[0])).group(1))
        d, m, y = str(r[6]).strip().split(".")
        t = out.setdefault(page, {"id": str(r[4]).strip().lstrip("0"), "central": _split(r[5]),
                                  "date": f"{y}-{int(m):02d}-{int(d):02d}", "sender": str(r[1] or "").strip(),
                                  "commitment": []})
        t["commitment"] += [c for c in _split(r[7]) if c not in t["commitment"]]
    return out


def same_sender(got, want):
    """The same institution, not the same text: the engine returns the approved list's official name ('מרכז רפואי
    ע"ש רמב"ם'), the Excel a short one ('רמב"ם'). Both are resolved through the list's aliases; a name the list does
    not know falls back to exact text."""
    from labextract_ocr.institutions import resolve_text
    if got == want:
        return True
    a, b = resolve_text(got), resolve_text(want)
    return bool(a and b and a & b)


def score(form, t):
    """{field: [mark, ...]}: one mark per value (✓ filled correct, ✗ filled WRONG, ? for review with a proposed
    reading / candidate, · nothing found), never the value itself. Filled values are still PENDING human approval."""
    exam = form.examinations[0] if form.examinations else None
    out = defaultdict(list)
    if exam is None:
        return out
    done = lambda fv: fv.extraction_status.value == COMPLETE and fv.normalized  # noqa: E731
    open_ = lambda fv: "?" if (fv.normalized or fv.raw or fv.candidates) else "·"  # noqa: E731
    # A = approved automatically and correct, X = approved automatically and WRONG (the critical number)
    mark = lambda fv, ok: ("A" if ok else "X") if fv.approval_status.value == APPROVED else ("✓" if ok else "✗")  # noqa: E731
    for field, fv, want in (("id", exam.israeli_id, t["id"]), ("date", exam.sample_received_date, t["date"]),
                            ("sender", exam.sender, t["sender"])):
        got = (fv.normalized.lstrip("0") if field == "id" else fv.normalized) if done(fv) else None
        ok = got is not None and (same_sender(got, want) if field == "sender" else got == want)
        out[field].append(open_(fv) if got is None else mark(fv, ok))
    for field, fvs, want in (("central", exam.central_numbers, t["central"]),
                             ("commitment", [c.number for c in exam.commitments], t["commitment"])):
        for fv in fvs:
            out[field].append(mark(fv, fv.normalized.lstrip("0") in want) if done(fv) else open_(fv))
        if not fvs:
            out[field].append("·")
    for field, fv in (("first_name", exam.first_name), ("last_name", exam.last_name)):
        out[field].append("proposal" if fv.normalized else "·")
    return out


def run(mode, pages, T, part):
    os.environ.update(LABEXTRACT_ENGINE="ocr", LABEXTRACT_LABX="on", LABX_MODE=mode, LABX_AUTO_APPROVE="1")
    from backend.readiness import apply_auto_approval
    from extraction import engine_select, labx_overlay
    from labx import clients
    calls, sent = Counter(), Counter()
    real_call = clients._gemini_call

    def counted(model, *a, **k):
        calls[model] += 1
        return real_call(model, *a, **k)
    clients._gemini_call = counted
    real_run, real_crops = labx_overlay._run, labx_overlay._run_crops

    def full(*a):
        sent["full_page"] += 1
        return real_run(*a)

    def crop(*a):
        outcome, res = real_crops(*a)
        if outcome == "sent":
            sent["crops_only"] += 1
        return outcome, res
    labx_overlay._run, labx_overlay._run_crops = full, crop
    content = PARTS[part][0].read_bytes()
    file_id = f"measure{part}" + "0" * 24
    per_page, t0 = {}, time.monotonic()
    try:
        for p in pages:
            form = apply_auto_approval(engine_select.extract_page(file_id, content, ".pdf", p - 1))
            per_page[p] = score(form, T[p])
            print(f"[{mode}] p{p:02d} " + "  ".join(f"{f}={''.join(m)}" for f, m in per_page[p].items()), flush=True)
    finally:
        clients._gemini_call = real_call
        labx_overlay._run, labx_overlay._run_crops = real_run, real_crops
    return {"per_page": per_page, "calls": dict(calls), "sent": dict(sent), "seconds": round(time.monotonic() - t0)}


def summary(mode, r):
    tot = defaultdict(Counter)
    for marks in r["per_page"].values():
        for f, ms in marks.items():
            tot[f].update(ms)
    lines = [f"## {mode}", "",
             "approved = approved automatically, nobody touches it; filled = entered and locked, waits for a click", "",
             "| field | approved correct | **approved WRONG** | filled correct | filled WRONG | review (proposed) | "
             "nothing found |", "|---|---|---|---|---|---|---|"]
    for f in ("id", "date", "central", "commitment", "sender"):
        c = tot[f]
        lines.append(f"| {f} | {c['A']} | **{c['X']}** | {c['✓']} | **{c['✗']}** | {c['?']} | {c['·']} |")
    for f in ("first_name", "last_name"):
        lines.append(f"| {f} | proposals: {tot[f]['proposal']} | (never accepted) | {tot[f]['·']} |")
    lines += ["", f"- pages sent as crops only: {r['sent'].get('crops_only', 0)}, full page: {r['sent'].get('full_page', 0)}",
              f"- real model calls: {sum(r['calls'].values())} {r['calls']}", f"- time: {r['seconds']} s", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", type=int, default=1, choices=sorted(PARTS))
    ap.add_argument("--modes", default="full_page,crops_first")
    ap.add_argument("--pages", default="")
    ap.add_argument("--out", default=str(ROOT / "out_measure"))
    a = ap.parse_args()
    T = truth(a.part)
    pages = [int(x) for x in a.pages.split(",")] if a.pages else sorted(T)
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    report = [f"# Privacy measurement, part {a.part}, pages {pages[0]}-{pages[-1]} ({len(pages)}), {time.strftime('%Y-%m-%d %H:%M')}", ""]
    for mode in a.modes.split(","):
        r = run(mode, pages, T, a.part)
        (out / f"{mode}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        report.append(summary(mode, r))
        print(summary(mode, r), flush=True)
    (out / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(f"report: {out / 'report.md'}")


if __name__ == "__main__":
    main()
