"""Switches the lab turns on / off by editing one file in GitHub: the commit redeploys the site (Vercel), and the
server reads the file when it starts.

backend/auto_approve.txt: "on" / "off". On: values two different models agreed on (labx consensus, every check
passed) are approved automatically (AUTO_APPROVED with rule id + policy version, backend/readiness.py); values only
the OCR read, and names, stay PENDING. An environment variable LABX_AUTO_APPROVE ("1" / "0") wins over the file.
"""
from __future__ import annotations

import os
from pathlib import Path

AUTO_APPROVE_FILE = Path(__file__).with_name("auto_approve.txt")


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
    print(f"[switches] auto-approve={'on' if os.environ['LABX_AUTO_APPROVE'] == '1' else 'off'}", flush=True)
