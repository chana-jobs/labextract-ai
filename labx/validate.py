"""Deterministic normalization + validation. The model reads; the code decides what is allowed."""
import datetime as dt
import re

HEB_NAME = re.compile(r"^[א-ת][א-ת'\"׳״\- ]{0,30}[א-ת'׳]$|^[א-ת]{2}$")

# Words that are labels / roles, never a patient name.
NOT_NAMES = {
    "שם", "פרטי", "משפחה", "שם פרטי", "שם משפחה", "ת.ז", "זכר", "נקבה", "מין", "בית", "חולים", "קופה", "כללית", "מכבי",
    "מאוחדת", "לאומית", "ד\"ר", "דר", "פרופ", "רופא", "מעבדה", "התקבל", "אחר", "הנבדק", "המטופל",
}


def norm_digits(s):
    return re.sub(r"\D", "", s or "")


def id_checksum_ok(digits):
    """Israeli ID check digit (Luhn variant) on the 9-digit zero-padded form."""
    if not digits or len(digits.lstrip("0")) == 0 or len(digits.lstrip("0")) > 9:
        return False
    d = digits.lstrip("0").zfill(9)
    total = 0
    for i, ch in enumerate(d):
        v = int(ch) * (1 if i % 2 == 0 else 2)
        total += v if v < 10 else v - 9
    return total % 10 == 0


def norm_id(raw):
    """Canonical comparison key for an ID: digits only, leading zeros removed. None if not plausibly an ID."""
    digits = norm_digits(raw)
    if raw and re.search(r"[A-Za-zא-ת]", raw):
        return None  # letters mixed in: not a clean ID reading
    if raw and "?" in raw:
        return None  # an unreadable digit: never drop it and compare the rest
    core = digits.lstrip("0")
    if not (5 <= len(core) <= 9):
        return None
    if len(digits) > 10:
        return None
    return core


ID_LABEL = re.compile(r"ת[.\"'״׳]?\s*ז|זהות|זהוי|דרכון|מ[.\"'״׳]\s*ז|\bID\b|PASS", re.IGNORECASE)


def id_label_ok(label):
    """The number must sit next to an ID label - otherwise it may be a case / sample / commitment number."""
    return bool(ID_LABEL.search(label or ""))


def norm_name(raw):
    s = re.sub(r"\s+", " ", (raw or "").strip()).strip(".,:;-_")
    s = s.replace("׳", "'").replace("״", '"')
    if not s or s in NOT_NAMES or not HEB_NAME.match(s):
        return None
    if any(w in NOT_NAMES for w in s.split()) and len(s.split()) == 1:
        return None
    return s


def parse_date(day, month, year):
    d, m, y = norm_digits(day), norm_digits(month), norm_digits(year)
    if not (d and m and y):
        return None
    if len(y) == 2:
        y = "20" + y
    if len(y) != 4:
        return None
    try:
        return dt.date(int(y), int(m), int(d))
    except ValueError:
        return None


def parse_loose_date(s):
    m = re.search(r"(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})", s or "")
    return parse_date(*m.groups()) if m else None


def date_plausible(received, form_dates, today=None):
    """Received date must be a real past date, not before the form's own issue/print/sample dates."""
    today = today or dt.date.today()
    if received is None or received > today or received < today - dt.timedelta(days=365):
        return False, "out_of_range"
    printed = [d for d in (parse_loose_date(x) for x in form_dates or []) if d]
    issued = [d for d in printed if d <= today]
    if issued and received < max(issued):
        return False, "before_form_date"
    if received in printed:
        # the same date is printed on the form (shipping / print / visit date): the reader may have taken it
        # instead of the receipt stamp - measured on the dev set, so it goes to review
        return False, "equals_printed_form_date"
    return True, ""
