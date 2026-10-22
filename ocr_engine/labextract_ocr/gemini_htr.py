"""Optional Gemini reader for fields the LOCAL engines located but did not resolve (22.10; OFF by default).

Targeted fallback: it sends only the crop of the specific place, with a prompt assembled from a FIXED template
library per (field x crop context). The prompt carries the crop's role / structure but NEVER a local reading,
so an agreement between Gemini and a local engine is an independent confirmation.
Never a source of truth and never an approver.
  - Gemini alone -> review candidate (PENDING).
  - Gemini 'full' == a local candidate of the same place -> two different engines agree -> may be proposed
    (PENDING), only when PROMOTION_ENABLED (off until measured on dev).
  - Received dates are promoted only on agreement with a local stamp read.
  - Patient names (22.10) are sent - only Gemini can read handwritten names - but they stay review candidates
    for good (NEVER_PROMOTE), like the patient ID.

Switches (environment). ALL gates must pass for a real call:
  LABOCR_GEMINI            off (default) | on     component active
  LABOCR_GEMINI_DRYRUN     on  (default) | off    on: plan + request are built, NOTHING is sent
  LABOCR_GEMINI_MAX_CALLS  0 (default)            hard cap of real API attempts per run (cost gate)
  LABOCR_GEMINI_BILLING_CHECKED  no (default) | yes   set only after the account owner verified that no billing
                           account is linked to the key's project (a missing credit card is NOT proof)
  LABOCR_GEMINI_PROMOTE    off (default) | on     agreement-based promotion (after the measured dev run)
  privacy gate: the source document's SHA-256 must be in data/gemini_dev_allowlist.json (anonymized dev samples).
The key is read in-process from python/.env by real_transport() only; it is never logged.

Requests: one batch per document, at most MAX_CROPS crops, a slot per kind (SLOTS), one ID and one name crop.
The response format is enforced twice: by the API's structured output (response_schema) and by validate().
An invalid response is discarded as a whole.
Errors: 503 -> one retry; 429 -> disabled for the WHOLE run; any other error -> no retry."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import cv2

from .textmatch import parse_date

ENABLED = os.environ.get("LABOCR_GEMINI", "off") == "on"
DRY_RUN = os.environ.get("LABOCR_GEMINI_DRYRUN", "on") != "off"
MAX_CALLS = int(os.environ.get("LABOCR_GEMINI_MAX_CALLS", "0"))
BILLING_CHECKED = os.environ.get("LABOCR_GEMINI_BILLING_CHECKED", "no") == "yes"
PROMOTION_ENABLED = os.environ.get("LABOCR_GEMINI_PROMOTE", "off") == "on"
MODEL = os.environ.get("LABOCR_GEMINI_MODEL", "gemini-3.1-flash-lite")
# backup model (used ONLY after a 503): gemini-3.5-flash-lite - verified on 22.10 with a real call on this key (answered in
# 1.7 s while the primary returned 503). gemini-2.5-flash is listed but returned 404 (4/4) - never use it.
# LABOCR_GEMINI_BACKUP_MODEL="" disables the backup.
BACKUP_MODEL = os.environ.get("LABOCR_GEMINI_BACKUP_MODEL", "gemini-3.5-flash-lite")
ENV_FILE = os.environ.get("LABOCR_GEMINI_ENV_FILE", "")      # optional .env holding GEMINI_API_KEY
ALLOWLIST = Path(__file__).resolve().parent.parent / "data" / "gemini_dev_allowlist.json"
MAX_CROPS = 6
TIMEOUT_S = 60
RETRY_WAIT_S = 20
STATUSES = ("full", "partial", "unreadable", "absent")

# kind -> (fields read from the crop, crop context used by the prompt library)
KINDS = {
    "central_and_date": (["central_number", "received_date"], "handwritten_annotation"),
    "received_date": (["received_date"], "handwritten_annotation"),
    "stamp_date": (["received_date"], "stamp"),
    "central_sticker": (["central_number"], "sticker"),
    "commitment_cell": (["commitment_number"], None),          # context from the region: table_cell | label_value
    "israeli_id": (["israeli_id"], "label_value"),
    "patient_name": (["first_name", "last_name"], "label_value"),  # 22.10: only Gemini can read handwritten names
}
SLOTS = {"central_and_date": 2, "commitment_cell": 2, "received_date": 1, "stamp_date": 1, "central_sticker": 1,
         "israeli_id": 1, "patient_name": 1}
SPARE_KINDS = ("central_and_date", "commitment_cell")           # never a 2nd patient (ID / name) crop

# --------------------------------------------------------------------------------------------- prompt library
PROMPT_COMMON = """You transcribe text in crops of scanned Hebrew medical forms. Each image has a crop_id, a context and a list of fields to read.
Rules for every field:
- Transcribe only characters that are visibly written or printed in this crop, in their order. Never guess, complete, correct or normalize, and never turn a partial reading into a known word, name or number.
- Do not use outside knowledge, typical formats, check digits or any other image as a hint.
- Write one '?' for each character that is present but cannot be read with certainty.
- status: "full" = every character read with certainty; "partial" = present but at least one '?'; "unreadable" = present but cannot be transcribed; "absent" = the field is not in this crop.
- text is null when status is "unreadable" or "absent".
- Return exactly one item for every requested (crop_id, field) pair and nothing else."""

PROMPT_CONTEXT = {
    "handwritten_annotation": "Context handwritten_annotation: handwriting added by the receiving lab (often a number with the words \"מספר מרכזי\" and a date under it).",
    "sticker": "Context sticker: a printed lab sticker; the number printed on it (often next to a barcode and a vertical label \"מרכזי\").",
    "stamp": "Context stamp: a rubber stamp of the receiving lab with the word \"התקבל\" and a date line.",
    "table_cell": "Context table_cell: one cell of a table column of numbers (the column header is not in the crop).",
    "label_value": "Context label_value: a value written or printed next to its label on a form.",
}
PROMPT_FIELD = {
    ("central_number", "handwritten_annotation"): "Field central_number: the handwritten lab number. Transcribe its digits exactly as written. Do not output any other number in the crop (commitment / request numbers, dates, phone numbers, printed numbers, barcode digits). If there is no handwritten lab number, status \"absent\".",
    ("central_number", "sticker"): "Field central_number: the number printed on the sticker, digits exactly as printed (not the barcode bars, not any other number).",
    ("received_date", "handwritten_annotation"): "Field received_date: the handwritten date written directly under or right next to the handwritten lab number. Copy it exactly as written, including its separators (for example 1/2/34 or 1.2.34). Do not use a printed date, a treatment, issue, print or transport date, or a date elsewhere in the crop. If there is no such handwritten date, status \"absent\" and text null.",
    ("received_date", "stamp"): "Field received_date: the date inside the \"התקבל\" stamp only, exactly as stamped, including its separators. Ignore every date outside the stamp. If the stamp date cannot be read, status \"unreadable\".",
    ("commitment_number", "table_cell"): "Field commitment_number: the number in this table cell, every digit exactly as shown, including leading zeros. Do not merge it with numbers from other cells or rows.",
    ("commitment_number", "label_value"): "Field commitment_number: the number written or printed as the value of the label in this crop, every digit exactly as shown, including leading zeros. Do not output dates, phone or license numbers.",
    ("first_name", "label_value"): "Field first_name: the patient's first name in this crop. Return ONLY the letters you actually recognize, in the script they are written in (Hebrew or English) and in their written order, one character per letter you see. Put '?' in the position of every letter you see but cannot identify. Never complete, correct or replace the letters with a known word or a common name, and never add or drop letters to make a real name. A scribble you cannot split into letters is \"unreadable\". Ignore other persons' names (physician, father) if visible.",
    ("last_name", "label_value"): "Field last_name: the patient's family name in this crop. Return ONLY the letters you actually recognize, in the script they are written in (Hebrew or English) and in their written order, one character per letter you see. Put '?' in the position of every letter you see but cannot identify. Never complete, correct or replace the letters with a known word or a common name, and never add or drop letters to make a real name. A scribble you cannot split into letters is \"unreadable\". Ignore other persons' names (physician, father) if visible.",
    ("israeli_id", "label_value"): "Field israeli_id: the digits of the ID number in this crop, exactly as written, including leading zeros. Do not correct digits, do not apply any check-digit logic and do not add or remove digits.",
}
FIELD_ENUM = ["central_number", "received_date", "israeli_id", "commitment_number", "first_name", "last_name"]
RESPONSE_SCHEMA = {
    "type": "OBJECT", "required": ["items"],
    "properties": {"items": {"type": "ARRAY", "items": {
        "type": "OBJECT", "required": ["crop_id", "field", "status", "text"],
        "properties": {"crop_id": {"type": "INTEGER"},
                       "field": {"type": "STRING", "enum": FIELD_ENUM},
                       "status": {"type": "STRING", "enum": list(STATUSES)},
                       "text": {"type": "STRING", "nullable": True}}}}}}
SCHEMA_TEXT = ('Return JSON only, exactly: {"items":[{"crop_id":<int>,"field":"central_number"|"received_date"|'
               '"israeli_id"|"commitment_number"|"first_name"|"last_name","status":"full"|"partial"|"unreadable"|"absent",'
               '"text":<string or null>}]}')


def build_prompt(items) -> str:
    """Common rules + the context and field templates the items need. Only role / structure, never values."""
    contexts = list(dict.fromkeys(it["context"] for it in items))
    pairs = list(dict.fromkeys((f, it["context"]) for it in items for f in it["fields"]))
    return "\n\n".join([PROMPT_COMMON] + [PROMPT_CONTEXT[c] for c in contexts] +
                       [PROMPT_FIELD[p] for p in pairs] + [SCHEMA_TEXT])


# --------------------------------------------------------------------------------------------- ledger / gates
class Ledger:
    """Run-level accounting shared by all pages (RUN_LEDGER). Only counts, model, status code and latency."""

    def __init__(self):
        self.attempts, self.disabled = [], False

    def record(self, status, seconds, code=None, dry=False, model=None):
        self.attempts.append({"model": model or MODEL, "status": status, "code": code,
                              "seconds": round(seconds, 2), "dry_run": dry})

    def real_attempts(self) -> int:
        return sum(1 for a in self.attempts if not a["dry_run"] and a["status"] in ("ok", "error"))


RUN_LEDGER = Ledger()


def reset_run():
    global RUN_LEDGER
    RUN_LEDGER = Ledger()


_SHA = {}


def privacy_gate(source_path) -> tuple[bool, str]:
    if not source_path:
        return False, "no source document"
    p = Path(source_path)
    if p not in _SHA:
        _SHA[p] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""
    allowed = set(json.loads(ALLOWLIST.read_text(encoding="utf-8"))["sha256"]) if ALLOWLIST.exists() else set()
    return (_SHA[p] in allowed, "document on the anonymized dev allowlist" if _SHA[p] in allowed else
            "document NOT on the dev allowlist - real data needs a separate privacy approval")


def cost_gate(ledger: Ledger) -> tuple[bool, str]:
    if not BILLING_CHECKED:
        return False, "billing not confirmed by the account owner (LABOCR_GEMINI_BILLING_CHECKED)"
    if ledger.real_attempts() >= MAX_CALLS:
        return False, f"call cap reached ({MAX_CALLS} per run)"
    return True, "within the approved call cap"


# --------------------------------------------------------------------------------------------- planning
FIRST_ONLY, LAST_ONLY = {"שם פרטי"}, {"שם משפחה"}


def _name_fields(label) -> list[str]:
    """Which name parts a crop holds - from the label that located it (evidence), never from the reading."""
    if label in FIRST_ONLY:
        return ["first_name"]
    if label in LAST_ONLY:
        return ["last_name"]
    return ["first_name", "last_name"]


def _sendable(r) -> bool:
    q = r.get("quality")
    return bool(q and q.get("sendable"))


def plan(fields: dict) -> tuple[list[dict], list[dict]]:
    """(items to send, held-back regions with the reason). Only LOCATED, UNRESOLVED fields whose region passed the
    quality check. Slots per kind; spare capacity only for SPARE_KINDS; at most one patient crop; never names."""
    want, skipped = {k: [] for k in KINDS}, []

    def offer(kind, r, context=None):
        (want[kind] if _sendable(r) else skipped).append({**r, "kind": kind, "context": context or KINDS[kind][1]})
    cen, rd, pid = fields["central_numbers"], fields["received_date"], fields["israeli_id"]
    com = fields.get("commitment_numbers", {"status": "proposed"})
    if cen["status"] != "proposed":
        for r in cen.get("regions", []):
            offer("central_sticker" if r.get("context") == "sticker" else "central_and_date", r)
    if rd["status"] != "proposed":
        for r in rd.get("regions", []):
            if r.get("context") == "stamp":
                offer("stamp_date", r)
            # 'received_date' under a proposed printed sticker: not sent (on dev it matched a referral date, p18)
    if com["status"] != "proposed" or com.get("count_uncertain"):
        for r in com.get("regions", []):
            offer("commitment_cell", r, r.get("context", "label_value"))
    nm = fields.get("first_name", {"status": "proposed"})
    if nm["status"] != "proposed" or fields.get("last_name", {}).get("status") != "proposed":
        regs = nm.get("regions", []) or fields.get("last_name", {}).get("regions", [])
        tight = [r for r in regs if not r.get("whole_block")]
        for r in tight or regs:
            offer("patient_name", r)
            if want["patient_name"] and want["patient_name"][-1]["bbox"] == r["bbox"]:
                want["patient_name"][-1]["name_fields"] = _name_fields(r.get("label"))
    if pid["status"] != "proposed":
        regs = pid.get("regions", [])
        tight = [r for r in regs if not r.get("whole_block")]
        for r in sorted(tight, key=lambda r: -(r.get("quality") or {}).get("digit_tokens", 0)) + \
                [r for r in regs if r.get("whole_block")]:
            offer("israeli_id", r)
    chosen, seen = [], set()

    def take(r):
        key = tuple(int(v) for v in r["bbox"])
        if key not in seen and len(chosen) < MAX_CROPS:
            seen.add(key); chosen.append(r)
    for kind, n in SLOTS.items():
        for r in want[kind][:n]:
            take(r)
    for kind in SPARE_KINDS:
        for r in want[kind][SLOTS[kind]:]:
            take(r)
    items = [{"crop_id": i, "kind": r["kind"], "fields": r.get("name_fields") or KINDS[r["kind"]][0],
              "context": r["context"],
              "bbox": [int(v) for v in r["bbox"]], "item_key": r.get("item_key"),
              "region_evidence": r.get("evidence", "")} for i, r in enumerate(chosen, 1)]
    skipped = [{"kind": r["kind"], "bbox": [int(v) for v in r["bbox"]], "why": (r.get("quality") or {}).get("why")}
               for r in skipped]
    return items, skipped


def crops(items, gray, views, pad=10) -> list[bytes]:
    img, k = gray, 1.0
    hires = views["hires"]() if views and views.get("hires") else None
    if hires is not None:
        img, k = hires, hires.shape[1] / gray.shape[1]
    H, W = img.shape[:2]
    out = []
    for it in items:
        x0, y0, x1, y1 = it["bbox"]
        c = img[max(0, int((y0 - pad) * k)):int((y1 + pad) * k), max(0, int((x0 - pad) * k)):int((x1 + pad) * k)]
        assert c.shape[0] < 0.5 * H and c.shape[1] < 0.8 * W, "refusing to send a page-sized crop"
        out.append(cv2.imencode(".png", c)[1].tobytes())
    return out


def request(items, images) -> dict:
    return {"model": MODEL, "prompt": build_prompt(items), "images": images, "schema": RESPONSE_SCHEMA,
            "parts": [{"crop_id": it["crop_id"], "context": it["context"], "fields": it["fields"]} for it in items]}


def request_summary(req) -> dict:
    return {"model": req["model"], "n_images": len(req["images"]), "parts": req["parts"],
            "bytes": sum(len(b) for b in req["images"])}


# --------------------------------------------------------------------------------------------- calling
def call(req, transport, ledger: Ledger, sleep=time.sleep, can_attempt=None):
    """Attempt 1 on the request's model. ONLY after a 503 and only if a verified BACKUP_MODEL is configured: one more
    attempt on it (after RETRY_WAIT_S); without a backup there is no second attempt.
    EVERY attempt - the retry / backup included - must pass can_attempt() (the run-level call cap) first.
    429 -> the component is disabled for the whole run. Auth / billing / bad request / any other error -> stop.
    transport(req, timeout_s) -> text, or raises with `.code`. The exception message is never logged."""
    models = [req["model"]] + ([BACKUP_MODEL] if BACKUP_MODEL and BACKUP_MODEL != req["model"] else [])
    for attempt, model in enumerate(models, 1):
        if ledger.disabled:
            return None
        if can_attempt is not None and not can_attempt():
            ledger.record("refused_by_cap", 0.0, dry=True, model=model)
            return None
        t0 = time.perf_counter()
        try:
            text = transport({**req, "model": model}, TIMEOUT_S)
            ledger.record("ok", time.perf_counter() - t0, model=model)
            return text
        except Exception as exc:
            code = getattr(exc, "code", None)
            ledger.record("error", time.perf_counter() - t0, code, model=model)
            if code == 429:
                ledger.disabled = True
                return None
            if code != 503 or attempt == len(models):
                return None
            sleep(RETRY_WAIT_S)
    return None


# --------------------------------------------------------------------------------------------- validation
NAME_RE = re.compile(r"^[א-תA-Za-z'\"?.\- ]+$")       # Hebrew letters, geresh, '?', hyphen, space
TEXT_RE = {"first_name": NAME_RE, "last_name": NAME_RE, "central_number": re.compile(r"^[0-9?]+$"), "israeli_id": re.compile(r"^[0-9?]+$"),
           "commitment_number": re.compile(r"^[0-9?]+$"),
           "received_date": re.compile(r"^[0-9?]{1,2}\s?[./-]\s?[0-9?]{1,2}(\s?[./-]\s?[0-9?]{2,4})?$")}


def validate(text, items) -> tuple[list[dict] | None, list[str]]:
    """Strict: {"items":[{crop_id:int, field:str, status:enum, text:str|null}]} with exactly the requested
    (crop_id, field) pairs - no missing, duplicate or extra item. Returns (items, []) or (None, errors)."""
    errs = []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None, ["not JSON"]
    if not isinstance(data, dict) or set(data) != {"items"} or not isinstance(data["items"], list):
        return None, ["top level must be exactly {\"items\": [...]}"]
    want = {(it["crop_id"], f) for it in items for f in it["fields"]}
    seen = set()
    for x in data["items"]:
        if not isinstance(x, dict) or set(x) != {"crop_id", "field", "status", "text"}:
            errs.append(f"item keys must be crop_id, field, status, text: {sorted(x) if isinstance(x, dict) else x}")
            continue
        key = (x["crop_id"], x["field"])
        if not isinstance(x["crop_id"], int) or isinstance(x["crop_id"], bool) or key not in want:
            errs.append(f"unrequested item {key}")
        elif key in seen:
            errs.append(f"duplicate item {key}")
        seen.add(key)
        if x["status"] not in STATUSES:
            errs.append(f"bad status {x['status']!r}")
        t = x["text"]
        if x["status"] in ("unreadable", "absent"):
            if t is not None:
                errs.append(f"{key}: text must be null for status {x['status']}")
        else:
            if not isinstance(t, str) or not t.strip():
                errs.append(f"{key}: text required for status {x['status']}")
            elif key[1] in TEXT_RE and not TEXT_RE[key[1]].match(t.strip()):
                errs.append(f"{key}: text has characters not allowed for {key[1]}")
            elif x["status"] == "full" and "?" in t:
                errs.append(f"{key}: '?' with status full")
            elif x["status"] == "partial" and "?" not in t:
                errs.append(f"{key}: status partial without '?'")
    missing = want - seen
    if missing:
        errs.append(f"missing items {sorted(missing)}")
    return (None, errs) if errs else (data["items"], [])


# --------------------------------------------------------------------------------------------- merging
def _normalize(field, raw):
    """Local normalization / checks, separate from the raw transcription."""
    t = raw.strip()
    if field in ("central_number", "israeli_id", "commitment_number"):
        v = re.sub(r"\D", "", t)
        checks = {"digits": len(v)}
        if field == "central_number":
            checks["six_digits"] = len(v) == 6
        return v, checks
    if field in ("first_name", "last_name"):
        return t, {"letters": len(t.replace(" ", ""))}
    iso = parse_date(t)
    parts = re.findall(r"\d+", t)
    checks = {"parses": bool(iso), "year_in_range": bool(iso) and 2020 <= int(iso[:4]) <= 2035,
              "two_digit_year": len(parts) == 3 and len(parts[2]) <= 2,
              "day_month_ambiguous": len(parts) >= 2 and parts[0] != parts[1]
              and int(parts[0]) <= 12 and int(parts[1]) <= 12}
    return iso, checks


TARGET = {"central_number": "central_numbers", "received_date": "received_date", "israeli_id": "israeli_id",
          "commitment_number": "commitment_numbers", "first_name": "first_name", "last_name": "last_name"}
NEVER_PROMOTE = {"israeli_id", "first_name", "last_name"}      # patient identity: review only


def _local_agree(f, field, value, it):
    """Local candidates (another engine) with the same value - for a commitment, of the SAME row (item_key)."""
    out = []
    x0, y0, x1, y1 = it["bbox"]
    for c in f["candidates"]:
        if c.get("source") == "gemini" or c.get("value") != value:
            continue
        b = c.get("bbox")
        if not b or b[2] < x0 or b[0] > x1 or b[3] < y0 or b[1] > y1:
            continue                                      # the same value elsewhere is NOT the same place
        if field == "commitment_number" and it.get("item_key") and c.get("item") != it["item_key"]:
            continue
        if field == "received_date" and c.get("form") == 2:
            continue                                      # dates: only a STAMP read counts as the second reader
        if field == "central_number" and it.get("context") == "sticker" and not (
                c.get("tier") in ("strong", "medium") or any("barcode decoded" in e for e in c.get("evidence", []))):
            continue                                      # agreement proves the reading, not the ROLE
        out.append(c)
    return out


def merge(fields: dict, items, valid_items) -> list[dict]:
    """Every FULL reading -> a review candidate (source gemini, PENDING). Agreement with a local reading of the same
    place is recorded; promotion on agreement only when PROMOTION_ENABLED (never for patient IDs / names).
    Partial / unreadable / absent readings stay in the returned record only."""
    by_id = {it["crop_id"]: it for it in items}
    record = []
    for x in valid_items:
        it = by_id[x["crop_id"]]
        rec = {"crop_id": x["crop_id"], "field": x["field"], "status": x["status"], "raw": x["text"]}
        record.append(rec)
        if x["status"] != "full":
            continue
        value, checks = _normalize(x["field"], x["text"])
        rec["local_checks"] = checks
        if not value:
            continue
        f = fields[TARGET[x["field"]]]
        agree = _local_agree(f, x["field"], value, it)
        rec["agrees_with_local"] = bool(agree)
        cand = {"value": value, "raw": x["text"], "conf": None, "bbox": it["bbox"], "source": "gemini",
                "method": "gemini_htr", "model": MODEL, "gemini_status": x["status"], "local_checks": checks,
                "role": "weak", "stable": False, "approval": "PENDING", "context": it["context"],
                "evidence": [f"Gemini ({MODEL}) raw transcription '{x['text']}' (status full) of {x['field']} "
                             f"[{it['context']}] in a located region: {it['region_evidence'][:160]}",
                             f"local normalization '{value}', checks {checks}"]}
        if it.get("item_key"):
            cand["item"] = it["item_key"]
        if agree:
            cand["evidence"].append(f"agrees with an independent local reading of the same place "
                                    f"({agree[0].get('method', 'local OCR')}: '{agree[0].get('raw', value)}')")
        promotable = (PROMOTION_ENABLED and agree and x["field"] not in NEVER_PROMOTE
                      and f.get("approval") not in ("HUMAN_APPROVED", "AUTO_APPROVED")
                      and (x["field"] != "received_date" or checks.get("year_in_range")))
        if promotable:
            cand.update(role="strong", stable=True)
            cand["evidence"].append("two different engines agree -> proposed (PENDING human approval)")
            for c in agree:
                c["stable"] = True
                c.setdefault("evidence", []).append("confirmed by an independent Gemini reading of the same place")
            _promote(f, x["field"], value)
        else:
            cand["evidence"].append("candidate only (no automatic promotion)")
        f["candidates"].append(cand)
        if f["status"] == "missing":
            f.update(status="uncertain", reason=f"{x['field']} read by Gemini only - review")
    return record


def _promote(f, field, value):
    """Proposal on two-engine agreement. Lists keep their count flags; a commitment row becomes proposed in items."""
    if field == "received_date":
        if f["status"] != "proposed" and len({c["value"] for c in f["candidates"] if c.get("source") != "gemini"}) <= 1:
            f.update(status="proposed", value=value, reason="received date: Gemini and the local stamp read agree")
    elif field == "central_number":
        if not [c for c in f["candidates"] if c.get("role", c.get("tier")) == "strong" and c["value"] != value]:
            f.update(status="proposed", values=sorted(set(f.get("values") or []) | {value}),
                     reason="central number: Gemini and a local reading agree")
    elif field == "commitment_number":
        for itm in f.get("items", []):
            if value in itm["candidate_values"] and itm["status"] != "proposed":
                itm.update(status="proposed", value=value,
                           uncertainty=itm["uncertainty"] + ["resolved: Gemini agrees with one local reading"])


# --------------------------------------------------------------------------------------------- entry point
def run(fields, gray, views, transport=None, source_path=None, ledger: Ledger | None = None) -> dict:
    ledger = ledger or RUN_LEDGER
    if not ENABLED:
        return {"enabled": False}
    items, skipped = plan(fields)
    summ = {"enabled": True, "planned": len(items), "skipped": skipped}
    if not items:
        return summ
    req = request(items, crops(items, gray, views))
    summ["request"] = request_summary(req)
    ok_priv, why_priv = privacy_gate(source_path)
    ok_cost, why_cost = cost_gate(ledger)
    summ["gates"] = {"privacy": why_priv, "cost": why_cost, "dry_run": DRY_RUN, "transport": transport is not None,
                     "disabled_by_429": ledger.disabled}
    if DRY_RUN or transport is None or not ok_priv or not ok_cost or ledger.disabled:
        ledger.record("dry_run", 0.0, dry=True)
        summ["sent"] = False
        return summ
    text = call(req, transport, ledger, can_attempt=lambda: cost_gate(ledger)[0])
    summ["sent"] = True
    if text is None:
        summ["result"] = "no response"
        return summ
    valid, errs = validate(text, items)
    if valid is None:
        summ["result"] = "invalid response - discarded"
        summ["schema_errors"] = errs[:10]
        return summ
    summ["result"] = "valid"
    summ["readings"] = merge(fields, items, valid)
    return summ


KEY_VARS = ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3")
KEY_INVALID = {400, 401, 403}                       # the key itself is rejected
TRANSIENT = {503, 504, None}                        # overload / timeout (None = no HTTP status, e.g. ReadTimeout)


class KeyFailover:
    """Switch to the next configured API key (approved by the project manager, 22.10) when:
      - the current key is rejected (400/401/403) - that key is then skipped for good;
      - the service is overloaded / times out (503 / 504 / timeout) - the next key, cyclically.
    NEVER on 429: a quota / rate limit is not worked around by switching keys. The failing request is not re-sent
    here: the error returns to call(), so every attempt - with any key - is counted against the call cap."""

    def __init__(self, keys):
        self.keys, self.i, self.dead = [k for k in keys if k], 0, set()

    def current(self):
        return self.keys[self.i] if self.keys else None

    def on_error(self, code) -> bool:
        """True if another key was activated for the NEXT request."""
        if code == 429 or not self.keys:
            return False
        if code in KEY_INVALID:
            self.dead.add(self.i)
        elif code not in TRANSIENT:
            return False
        for step in range(1, len(self.keys)):
            j = (self.i + step) % len(self.keys)
            if j not in self.dead:
                self.i = j
                return True
        return False


def real_transport():
    """google-genai transport (REAL network call): structured output enforces the response schema server-side.
    Only for an explicitly approved run; never used by default."""
    import truststore
    from dotenv import load_dotenv
    from google import genai
    from google.genai import types
    truststore.inject_into_ssl()
    if not os.environ.get("GEMINI_API_KEY") and ENV_FILE:
        load_dotenv(Path(ENV_FILE))                       # key from LABOCR_GEMINI_ENV_FILE; never logged
    keys = KeyFailover([os.environ.get(v) for v in KEY_VARS])
    if not keys.current():
        raise RuntimeError("GEMINI_API_KEY is not set (environment variable, or a .env file via LABOCR_GEMINI_ENV_FILE)")
    clients = {}

    def client():
        if keys.i not in clients:
            clients[keys.i] = genai.Client(api_key=keys.current(), http_options=types.HttpOptions(
                timeout=TIMEOUT_S * 1000, retry_options=types.HttpRetryOptions(attempts=1)))
        return clients[keys.i]

    def send(req, timeout_s):
        parts = [types.Part.from_text(text=req["prompt"])]
        for p, img in zip(req["parts"], req["images"]):
            parts += [types.Part.from_text(text=f"crop_id {p['crop_id']} - context {p['context']} - fields to read: "
                                                f"{', '.join(p['fields'])}"),
                      types.Part.from_bytes(data=img, mime_type="image/png")]
        try:
            resp = client().models.generate_content(model=req["model"], contents=parts, config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=req["schema"], temperature=0))
        except Exception as exc:
            keys.on_error(getattr(exc, "code", None))     # next key for the NEXT request (never on 429)
            raise
        return resp.text
    send.keys = keys                                      # index only, for the ledger - never the key value
    return send
