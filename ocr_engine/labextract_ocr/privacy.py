"""Print-footer masking (PRIVACY_REVIEW.md, permanent rule). A footer row such as
"הודפס מאתר … עבור <name> תעודת זהות: <id>" identifies the person who printed the form: it is masked before
any cache write, output or log, and excluded from field assignment (it is never the patient)."""
from __future__ import annotations

from .layout import rows as make_rows
from .textmatch import norm_heb

MASK = "[REDACTED]"


def is_print_footer(row) -> bool:
    text = norm_heb("".join(t.text for t in row))
    return "הודפס" in text and ("עבור" in text or "זהות" in text)


def mask_print_footer(tokens) -> tuple[list, int]:
    """Tokens with every print-footer row's text replaced by MASK (boxes kept). Returns (tokens, n_masked)."""
    ids = {t.id for row in make_rows(tokens) if is_print_footer(row) for t in row}
    for t in tokens:
        if t.id in ids:
            t.text = MASK
    return tokens, len(ids)
