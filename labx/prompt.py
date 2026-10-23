"""The one extraction prompt + JSON schema sent to every reader model."""

PROMPT = """You are reading ONE scanned page of an Israeli medical "commitment" form (התחייבות) that arrived at the
Central Virology Laboratory. The page may be rotated, faint, photocopied, or partly handwritten. Fill the JSON.

PATIENT (the person tested - not the physician, not the father, not the clerk who printed the form, not the issuer):
- patient_first_name / patient_last_name: the patient's name in Hebrew, often handwritten in colored pen in FLUENT
  HEBREW CURSIVE (כתב יד רהוט) - convert each cursive letter to its printed letter. If only one combined
  "full name" field exists, the first word is usually the first name - but if you cannot tell which part is which,
  leave both empty.
- patient_id: the patient's ID number (ת.ז. / תעודת זהות / מספר זהות / ID / PASS / מ.ז.). Copy EVERY digit exactly as
  written, including leading zeros. Do not add, drop, or "fix" digits. A case number (מקרה), sample number (דגימה),
  request/commitment number, phone, or barcode number is NOT the ID.
- patient_id_label: the printed label right next to the ID, copied exactly (e.g. "ת.ז.", "מספר זהות", "ID / PASS").
  If the number has no printed label next to it, return "".

RECEIVED DATE (the date the lab received the sample):
- Usually inside a rectangular rubber stamp with the word "התקבל" and "המעבדה המרכזית לנגיפים"; the date is printed like
  "18 -06- 2026". Sometimes it is handwritten inside/near an empty stamp frame, or handwritten under a handwritten
  central number (e.g. "939158" with "16/6/26" below it).
- NOT the birth date, NOT the print/issue date, NOT the validity date, NOT the sample-collection date, NOT a fax header.
- Give day, month, year exactly as written (year as written: "2026", "26", ...).
- received_date_source: "stamp" (inside the התקבל stamp), "handwritten" (handwritten receipt date), or "none".

OTHER: form_dates = every other printed date on the page that is an issue / print / visit / sample date
(exclude birth dates and "valid until" dates), as dd/mm/yyyy.

ABSOLUTE RULES:
- Never guess or complete. If you cannot read a value with certainty, return "" for it.
- legibility per field: "clear" ONLY if every single character is unambiguous to you; "unsure" if any character is a
  guess; "none" if the field is absent or unreadable (then the value is "").
- Do not use outside knowledge of typical names; transcribe only what is written.
"""

_FIELD = {"type": "string"}
_LEG = {"type": "string", "enum": ["clear", "unsure", "none"]}

SCHEMA = {
    "type": "object",
    "properties": {
        "patient_first_name": _FIELD,
        "patient_first_name_legibility": _LEG,
        "patient_last_name": _FIELD,
        "patient_last_name_legibility": _LEG,
        "patient_id": _FIELD,
        "patient_id_legibility": _LEG,
        "patient_id_label": _FIELD,
        "received_day": _FIELD,
        "received_month": _FIELD,
        "received_year": _FIELD,
        "received_date_legibility": _LEG,
        "received_date_source": {"type": "string", "enum": ["stamp", "handwritten", "none"]},
        "form_dates": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "patient_first_name", "patient_first_name_legibility", "patient_last_name", "patient_last_name_legibility",
        "patient_id", "patient_id_legibility", "patient_id_label", "received_day", "received_month", "received_year",
        "received_date_legibility", "received_date_source", "form_dates",
    ],
    "additionalProperties": False,
}
