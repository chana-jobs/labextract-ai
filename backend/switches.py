"""Switches the lab turns on / off by editing one file in GitHub: the commit redeploys the site (Vercel), and the
server reads the file when it starts.

backend/auto_approve.txt: "on" / "off". On: values confirmed by two independent sources (two different models;
the OCR + a barcode; the OCR + two models reading its crop) are approved automatically (AUTO_APPROVED with rule id +
policy version, backend/readiness.py); a value with one source, and names, stay PENDING.
An environment variable LABX_AUTO_APPROVE ("1" / "0") wins over the file.

backend/crops_first.txt: "on" / "off". On: only regions located locally are sent to the model, the full page only
when a field could not be located (LABX_MODE=crops_first). The environment variable LABX_MODE wins over the file.
"""
from __future__ import annotations

import os
from pathlib import Path

AUTO_APPROVE_FILE = Path(__file__).with_name("auto_approve.txt")
CROPS_FIRST_FILE = Path(__file__).with_name("crops_first.txt")


def auto_approve_from_file(path: Path = AUTO_APPROVE_FILE) -> bool:
    """The first word that is not a comment: "on" turns it on; anything else, or a missing file, is off."""
    try:
        words = [w for line in path.read_text(encoding="utf-8").splitlines()
                 if (w := line.strip()) and not w.startswith("#")]
    except OSError:
        return False
    return words[:1] == ["on"]


def load_switches() -> None:
    if "LABX_AUTO_APPROVE" not in os.environ:
        os.environ["LABX_AUTO_APPROVE"] = "1" if auto_approve_from_file() else "0"
    if "LABX_MODE" not in os.environ:
        os.environ["LABX_MODE"] = "crops_first" if auto_approve_from_file(CROPS_FIRST_FILE) else "full_page"
    print(f"[switches] auto-approve={'on' if os.environ['LABX_AUTO_APPROVE'] == '1' else 'off'} "
          f"mode={os.environ['LABX_MODE']}", flush=True)
