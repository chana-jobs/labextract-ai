"""Rule-based extraction from the TEXT LAYER of a digital PDF (PyMuPDF). No OCR, no model, no AI.

What it does: finds the seven fields next to their printed labels (Hebrew or English) and reads
values from the same line, or from the next line when the label line has no value. Every value
carries the page and the bounding box of the text line it came from (exact for a text layer).

What it never does: invent a value, pad a digit, pick between conflicting readings, or read a
scanned page. A page without a text layer gets no values and a count issue, so a person enters
the data from the source. Values are proposals (PENDING); approval is always a separate step.

Validated only on synthetic PDFs. Real laboratory templates are [To verify] (label wording,
layout, right-to-left text order inside the PDF).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import pymupdf

from shared.contracts import (
    Attribution, Candidate, Commitment, Dimensions, Evidence, Examination, ExistenceStatus,
    ExtractionStatus, FieldName, FieldValue, Form, Issue, IssueReason, ProcessingStatus, Readability,
    SplitStatus, UncertaintyReason, Verification,
)

ENGINE_VERSION = "pdf-text-0.1"
METHOD = "pdf_text_layer"
MIN_TEXT_CHARS = 20          # fewer characters on a page = treated as having no usable text layer

LABELS: dict[FieldName, tuple[str, ...]] = {
    FieldName.FIRST_NAME: ("שם פרטי", "first name"),
    FieldName.LAST_NAME: ("שם משפחה", "last name", "surname"),
    FieldName.ISRAELI_ID: ("תעודת זהות", "מספר זהות", "ת.ז.", "ת.ז", 'ת"ז', "ת״ז", "israeli id", "id number"),
    FieldName.SAMPLE_RECEIVED_DATE: ("תאריך קבלת הדגימה", "תאריך קבלת דגימה", "תאריך קבלה",
                                     "sample received date", "date received"),
    FieldName.SENDER: ("גורם שולח", "שולח", "sender"),
    FieldName.CENTRAL_NUMBERS: ("מספר מרכזי", "מס' מרכזי", "מס׳ מרכזי", "central number"),
    FieldName.COMMITMENT_NUMBER: ("מספר התחייבות", "מס' התחייבות", "מס׳ התחייבות", "commitment number"),
}
SCALAR_FIELDS = (FieldName.FIRST_NAME, FieldName.LAST_NAME, FieldName.ISRAELI_ID,
                 FieldName.SAMPLE_RECEIVED_DATE, FieldName.SENDER)


@dataclass(frozen=True)
class Line:
    index: int
    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class Hit:
    """A value segment found next to a label."""
    field: FieldName
    label: str
    text: str
    line: Line
    attribution: Attribution


@dataclass(frozen=True)
class Reading:
    raw: str
    normalized: str | None   # None: the text is there but cannot be read as a valid value
    flags: tuple[str, ...] = ()


# ------------------------------------------------------------------ text layer
def _lines(page) -> list[Line]:
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                lines.append(Line(len(lines), " ".join(text.split()), tuple(round(v, 2) for v in line["bbox"])))
    return lines


def _label_hits(line: Line) -> list[tuple[int, int, FieldName, str]]:
    """(start, end, field, label) for every label in the line; a label inside a longer one is ignored."""
    lowered = line.text.lower()
    found = []
    for field, labels in LABELS.items():
        for label in labels:
            for match in re.finditer(re.escape(label.lower()), lowered):
                found.append((match.start(), match.end(), field, label))
    found.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept = []
    for hit in found:
        if not any(k[0] <= hit[0] and hit[1] <= k[1] for k in kept):
            kept.append(hit)
    return kept


def _segments(lines: list[Line]) -> list[Hit]:
    hits = []
    for position, line in enumerate(lines):
        labels = _label_hits(line)
        for k, (start, end, field, label) in enumerate(labels):
            stop = labels[k + 1][0] if k + 1 < len(labels) else len(line.text)
            value = line.text[end:stop].strip(" :-–\t")
            if value:
                hits.append(Hit(field, label, value, line, Attribution.STRONG))
            elif k + 1 == len(labels) and position + 1 < len(lines) and not _label_hits(lines[position + 1]):
                # label alone at the end of its line: the value is the next line
                hits.append(Hit(field, label, lines[position + 1].text, lines[position + 1], Attribution.MEDIUM))
            else:
                hits.append(Hit(field, label, "", line, Attribution.WEAK))
    return hits


def _visual_order(lines: list[Line]) -> bool:
    """True when Hebrew labels appear only reversed (text layer in visual order)."""
    reversed_labels = [label[::-1] for labels in LABELS.values() for label in labels if not label.isascii()]
    return any(label in line.text for line in lines for label in reversed_labels)


# ------------------------------------------------------------------ value readers
_DIGITS = re.compile(r"[0-9]+")


def _read_name(text: str) -> list[Reading]:
    value = text.strip(" .,;:")
    if value and not any(ch.isdigit() for ch in value) and any(ch.isalpha() for ch in value) and len(value) <= 60:
        return [Reading(value, value)]
    return [Reading(value, None)] if value else []


def _read_israeli_id(text: str) -> list[Reading]:
    tokens = _DIGITS.findall(text)
    nine = [t for t in tokens if len(t) == 9]
    if nine:
        return [Reading(t, t) for t in nine]
    # digits present but not 9 of them: never padded, never guessed
    return [Reading(t, None, ("FORMAT_INVALID",)) for t in tokens]


_DATE_DMY = re.compile(r"\b([0-9]{1,2})[./-]([0-9]{1,2})[./-]([0-9]{2,4})\b")
_DATE_ISO = re.compile(r"\b([0-9]{4})-([0-9]{2})-([0-9]{2})\b")


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _read_date(text: str) -> list[Reading]:
    readings = []
    for m in _DATE_ISO.finditer(text):
        readings.append(Reading(m.group(0), _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))))
    for m in _DATE_DMY.finditer(text):
        day, month, year_text = int(m.group(1)), int(m.group(2)), m.group(3)
        if len(year_text) != 4:
            readings.append(Reading(m.group(0), None, ("DATE_AMBIGUOUS", "two_digit_year")))
            continue
        iso = _iso(int(year_text), month, day)
        if day <= 12 and month <= 12 and day != month:
            # DD/MM and MM/DD both valid (Spec 5): proposed as a candidate, never as the value
            readings.append(Reading(m.group(0), None, ("DATE_AMBIGUOUS",) + ((f"dmy:{iso}",) if iso else ())))
        else:
            readings.append(Reading(m.group(0), iso))
    return readings


def _read_numbers(text: str, min_len: int) -> list[Reading]:
    return [Reading(t, t) for t in _DIGITS.findall(text) if len(t) >= min_len]


READERS = {
    FieldName.FIRST_NAME: _read_name,
    FieldName.LAST_NAME: _read_name,
    FieldName.SENDER: _read_name,
    FieldName.ISRAELI_ID: _read_israeli_id,
    FieldName.SAMPLE_RECEIVED_DATE: _read_date,
    FieldName.CENTRAL_NUMBERS: lambda text: _read_numbers(text, 4),
    FieldName.COMMITMENT_NUMBER: lambda text: _read_numbers(text, 5),
}


# ------------------------------------------------------------------ building contract objects
def _evidence(page: int, hit: Hit) -> Evidence:
    return Evidence(page=page, bbox=list(hit.line.bbox), method=METHOD, engine_version=ENGINE_VERSION,
                    role_basis=f"label:{hit.label}", tier=hit.attribution)


def _dimensions(attribution: Attribution) -> Dimensions:
    return Dimensions(readability=Readability.OK, attribution=attribution, verification=Verification.NONE)


def _value(field: FieldName, page: int, found: list[tuple[Hit, Reading]], label_seen: bool) -> FieldValue:
    readable = [(h, r) for h, r in found if r.normalized]
    distinct = {r.normalized for _, r in readable}

    if len(distinct) == 1 and len(readable) == len(found):
        hit, reading = readable[0]
        evidence = [_evidence(page, h) for h, r in readable]
        return FieldValue(field_name=field, raw=reading.raw, normalized=reading.normalized,
                          extraction_status=ExtractionStatus.COMPLETE, dimensions=_dimensions(hit.attribution),
                          evidence=evidence)

    candidates = [
        Candidate(raw=r.raw, normalized=r.normalized or next((f[4:] for f in r.flags if f.startswith("dmy:")), None),
                  tier=h.attribution, role_basis=f"label:{h.label}", evidence=[_evidence(page, h)])
        for h, r in found
    ]
    flags = sorted({f for _, r in found for f in r.flags if not f.startswith("dmy:")})

    if len(distinct) > 1:
        return FieldValue(field_name=field, extraction_status=ExtractionStatus.CONFLICT, candidates=candidates,
                          uncertainty_reason=[UncertaintyReason.CANDIDATE_CONFLICT], flags=flags)
    if found or label_seen:
        # the label is printed, but no valid value can be read next to it: unreadable, not absent
        return FieldValue(field_name=field, extraction_status=ExtractionStatus.UNCERTAIN, candidates=candidates,
                          uncertainty_reason=[UncertaintyReason.RECOGNITION], flags=flags)
    return FieldValue(field_name=field, extraction_status=ExtractionStatus.MISSING,
                      uncertainty_reason=[UncertaintyReason.UNGROUNDED])


def _unread(field: FieldName, flag: str) -> FieldValue:
    return FieldValue(field_name=field, extraction_status=ExtractionStatus.UNCERTAIN,
                      uncertainty_reason=[UncertaintyReason.RECOGNITION], flags=[flag])


def _form_without_text(file_id: str, page: int, note: str, flag: str = "no_text_layer") -> Form:
    form_id = f"{file_id}-p{page + 1}"
    exam_id = f"{form_id}-e1"
    exam = Examination(examination_id=exam_id, form_id=form_id,
                       **{f.value: _unread(f, flag) for f in SCALAR_FIELDS})
    return Form(form_id=form_id, file_id=file_id, page_regions=[{"page": page}], split_status=SplitStatus.UNCERTAIN,
                processing_status=ProcessingStatus.DONE, examinations=[exam],
                issues=[Issue(issue_id=f"{form_id}-count", form_id=form_id,
                              reason=IssueReason.COMMITMENT_COUNT_UNCERTAIN, note=note)])


def extract_page(file_id: str, page_number: int, page) -> Form:
    form_id = f"{file_id}-p{page_number + 1}"
    exam_id = f"{form_id}-e1"
    lines = _lines(page)
    if sum(len(line.text) for line in lines) < MIN_TEXT_CHARS:
        return _form_without_text(file_id, page_number,
                                  "No text layer on this page: OCR is not available; enter values from the source")

    hits = _segments(lines)
    if not hits and _visual_order(lines):
        # Hebrew stored right-to-left as left-to-right glyphs: reading it would scramble values
        return _form_without_text(file_id, page_number,
                                  "Text layer in visual (reversed) order is not supported; enter values from the source",
                                  flag="text_order_unsupported")
    by_field: dict[FieldName, list[tuple[Hit, Reading]]] = {f: [] for f in LABELS}
    for hit in hits:
        for reading in READERS[hit.field](hit.text):
            by_field[hit.field].append((hit, reading))
    seen = {hit.field for hit in hits}

    shared = {f.value: _value(f, page_number, by_field[f], f in seen) for f in SCALAR_FIELDS}

    central_numbers, seen_central = [], set()
    for hit, reading in by_field[FieldName.CENTRAL_NUMBERS]:
        if reading.normalized not in seen_central:
            seen_central.add(reading.normalized)
            central_numbers.append(FieldValue(
                field_name=FieldName.CENTRAL_NUMBERS, raw=reading.raw, normalized=reading.normalized,
                extraction_status=ExtractionStatus.COMPLETE, dimensions=_dimensions(hit.attribution),
                evidence=[_evidence(page_number, hit)]))

    issues: list[Issue] = []
    commitments: list[Commitment] = []
    commitment_hits = [h for h in hits if h.field == FieldName.COMMITMENT_NUMBER]
    numbers_seen: set[str] = set()
    for hit in commitment_hits:
        anchor = f"p{page_number}:line{hit.line.index}"
        readings = READERS[FieldName.COMMITMENT_NUMBER](hit.text) or [None]
        for reading in readings:
            if reading is not None and reading.normalized in numbers_seen:
                # the same number printed twice: one commitment or two cannot be told apart
                issues.append(Issue(issue_id=f"{form_id}-count", form_id=form_id,
                                    reason=IssueReason.COMMITMENT_COUNT_UNCERTAIN,
                                    note="The same commitment number appears more than once"))
                continue
            index = len(commitments)
            if reading is None:
                # a commitment label with no readable number: the row exists, its number is empty
                number = FieldValue(field_name=FieldName.COMMITMENT_NUMBER,
                                    extraction_status=ExtractionStatus.UNCERTAIN,
                                    uncertainty_reason=[UncertaintyReason.RECOGNITION])
            else:
                numbers_seen.add(reading.normalized)
                number = FieldValue(field_name=FieldName.COMMITMENT_NUMBER, raw=reading.raw,
                                    normalized=reading.normalized, extraction_status=ExtractionStatus.COMPLETE,
                                    dimensions=_dimensions(hit.attribution),
                                    evidence=[_evidence(page_number, hit)])
            commitments.append(Commitment(
                commitment_id=f"{form_id}-c{index + 1}", examination_id=exam_id,
                existence_status=ExistenceStatus.CONFIRMED, existence_evidence_ref=anchor,
                origin_anchor=anchor, internal_index=index, number=number))

    if not commitment_hits:
        issues.append(Issue(issue_id=f"{form_id}-count", form_id=form_id,
                            reason=IssueReason.COMMITMENT_COUNT_UNCERTAIN,
                            note="No commitment label found on this page; no row was created"))

    unique_issues = list({issue.issue_id: issue for issue in issues}.values())
    exam = Examination(examination_id=exam_id, form_id=form_id, central_numbers=central_numbers,
                       commitments=commitments, **shared)
    return Form(form_id=form_id, file_id=file_id, page_regions=[{"page": page_number}],
                split_status=SplitStatus.UNCERTAIN,     # one page = one form is an assumption, not verified
                processing_status=ProcessingStatus.DONE, examinations=[exam], issues=unique_issues)


def extract_document(file_id: str, content: bytes, extension: str) -> list[Form]:
    """One form per page. Images have no text layer: nothing is extracted from them."""
    if extension != ".pdf":
        return [_form_without_text(file_id, 0, "Image file: OCR is not available; enter values from the source")]
    with pymupdf.open(stream=content, filetype="pdf") as document:
        return [extract_page(file_id, number, page) for number, page in enumerate(document)]
