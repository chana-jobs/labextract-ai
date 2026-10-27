"""One live end-to-end check of a deployment with a SYNTHETIC scanned PDF: sign in -> upload -> OCR -> correct and
approve -> READY -> Excel. Run by .github/workflows/cloud-e2e.yml (a network outside the developers' filter).

Environment: LX_BASE_URL (deployment, no trailing slash), LX_EMAIL, LX_PASSWORD (an existing account),
VERCEL_BYPASS (optional: Protection Bypass for Automation secret of a protected Preview).
Prints only synthetic values; never prints credentials."""
import http.cookiejar
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import openpyxl

BASE = os.environ["LX_BASE_URL"].rstrip("/")
API = BASE + "/api"
HEADERS = {"Origin": BASE}
if os.environ.get("VERCEL_BYPASS"):
    HEADERS |= {"x-vercel-protection-bypass": os.environ["VERCEL_BYPASS"], "x-vercel-set-bypass-cookie": "true"}
SCAN = Path(__file__).with_name("fixtures") / "synthetic_scan.pdf"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
failed = False


def call(method, path, body=None, raw=None, ctype=None, base=API):
    headers = dict(HEADERS)
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    if raw is not None:
        data, headers["Content-Type"] = raw, ctype
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with opener.open(request, timeout=330) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def step(name, ok, info=""):
    global failed
    failed |= not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {info}", flush=True)
    return ok


s, b = call("GET", "/", base=BASE)
step("home page is the V2 review UI", s == 200 and "V2" in b.decode("utf-8", "ignore"), str(s))
s, b = call("GET", "/health")
step("API health", s == 200, b.decode()[:60])
s, _ = call("POST", "/auth/login", {"email": os.environ["LX_EMAIL"], "password": os.environ["LX_PASSWORD"]})
if not step("sign in", s == 200, str(s)):
    sys.exit(1)

boundary = uuid.uuid4().hex
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"synthetic_scan.pdf\"\r\n"
        "Content-Type: application/pdf\r\n\r\n").encode() + SCAN.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
s, b = call("POST", "/documents/upload", raw=body, ctype=f"multipart/form-data; boundary={boundary}")
if not step("upload synthetic scan", s == 200, str(s)):
    sys.exit(1)
file_id = json.loads(b)["file_id"]

started = time.time()
s, b = call("POST", f"/documents/{file_id}/drafts/init")
step("OCR in the cloud (drafts init)", s == 200, f"{s} in {time.time() - started:.0f}s {b.decode()[:200] if s != 200 else ''}")


def form():
    return json.loads(call("GET", f"/documents/{file_id}/drafts")[1])[0]


f = form()
exam = f["examinations"][0]
methods = sorted({ev["method"] for k in ("first_name", "last_name", "israeli_id") for ev in exam[k]["evidence"]})
commitments = [c["number"]["normalized"] for c in exam["commitments"]]
print(f"   status={f['processing_status']} methods={methods} issues={[i['reason'] for i in f['issues']]}")
print(f"   names/id={[exam[k]['normalized'] for k in ('first_name', 'last_name', 'israeli_id')]} commitments={commitments}")
step("OCR engine produced the values", "local_paddle_ocr" in methods)
step("commitment read from the scan", "730584234" in commitments, str(commitments))

for name, value in (("sample_received_date", "2026-10-01"), ("sender", "Synthetic Lab")):
    if not exam[name]["normalized"]:
        s, _ = call("PATCH", f"/review/{file_id}/{f['form_id']}/field",
                    {"target": {"field": name}, "value": value, "expected_version": exam[name]["version"]})
        step(f"reviewer fills {name}", s == 200, str(s))
if not exam["central_numbers"]:
    s, _ = call("PATCH", f"/review/{file_id}/{f['form_id']}/field",
                {"target": {"field": "central_numbers"}, "op": "add", "value": "4455", "expected_count": 0})
    step("reviewer adds central number", s == 200, str(s))

exam = form()["examinations"][0]
targets = [{"field": k} for k in ("first_name", "last_name", "israeli_id", "sample_received_date", "sender")]
targets += [{"field": "central_numbers", "index": i, "expected_value": c["normalized"]}
            for i, c in enumerate(exam["central_numbers"])]
targets += [{"field": "commitment_number", "commitment_id": c["commitment_id"]} for c in exam["commitments"]]
for target in targets:
    exam = form()["examinations"][0]
    if target["field"] == "central_numbers":
        version = exam["central_numbers"][target["index"]]["version"]
    elif target["field"] == "commitment_number":
        version = next(c["number"]["version"] for c in exam["commitments"]
                       if c["commitment_id"] == target["commitment_id"])
    else:
        version = exam[target["field"]]["version"]
    s, b = call("POST", f"/review/{file_id}/{f['form_id']}/field/approval",
                {"target": target, "action": "APPROVE", "expected_version": version})
    step(f"approve {target['field']}", s == 200, "" if s == 200 else b.decode()[:120])

s, b = call("GET", f"/documents/{file_id}/export/summary")
summary = json.loads(b)
print("   summary:", json.dumps(summary, ensure_ascii=False))
step("row READY", summary.get("ready") == 1)
s, b = call("GET", f"/documents/{file_id}/export.xlsx?confirm_partial=true")
if step("Excel download", s == 200 and b[:2] == b"PK", str(s)):
    for row in openpyxl.load_workbook(io.BytesIO(b)).worksheets[0].iter_rows(values_only=True):
        print("     ", row)
call("POST", "/auth/logout")
sys.exit(1 if failed else 0)
