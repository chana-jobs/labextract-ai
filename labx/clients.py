"""Vision model clients (Gemini free tier + OpenRouter free models) with on-disk response cache.

Every call is cached by (model, prompt, image hash), so re-running the evaluation costs no quota.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import truststore

truststore.inject_into_ssl()  # the network uses a local root CA; use the Windows cert store

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
# LABX_CACHE_DIR: writable folder for the response cache (on a server: a private temp folder)
CACHE_ROOT = Path(os.environ.get("LABX_CACHE_DIR") or ROOT / "cache")
CACHE_DIR = CACHE_ROOT / "responses"
ENV_FILE = os.environ.get("LABX_ENV_FILE", str(ROOT / ".env"))


def _env():
    vals = {}
    for f in [os.environ.get("LABX_EXTRA_ENV_FILE", r"C:\accthon_chani\python\.env"), ENV_FILE]:
        if Path(f).exists():
            vals.update({k: v for k, v in dotenv_values(f).items() if v})
    vals.update({k: v for k, v in os.environ.items() if k.endswith("_API_KEY") or "_API_KEY_" in k})
    return vals


ENV = _env()
def _clean(value):
    """Keys pasted through some shells arrive with a BOM / zero-width / whitespace around them; an HTTP header
    must be plain ASCII, so such a key fails every call. Keep only the visible ASCII characters."""
    return "".join(ch for ch in (value or "") if 33 <= ord(ch) <= 126)


def _gemini_keys():
    """GEMINI_API_KEY, GEMINI_API_KEY_2, _3, _4 ... (any number), duplicates removed, in order.
    LABX_EXCLUDE_KEYS="GEMINI_API_KEY_8,..." keeps keys out of this process (e.g. reserved for a live demo)."""
    import re
    excluded = {x.strip() for x in os.environ.get("LABX_EXCLUDE_KEYS", "").split(",") if x.strip()}
    reserved_values = {_clean(ENV.get(k)) for k in excluded}
    names = sorted((k for k in ENV if re.fullmatch(r"GEMINI_API_KEY(_\d+)?", k) and k not in excluded
                    and _clean(ENV.get(k)) not in reserved_values),
                   key=lambda k: int(k.rsplit("_", 1)[1]) if k[-1].isdigit() else 1)
    keys = []
    for k in names:
        v = _clean(ENV[k])
        if v and v not in keys:
            keys.append(v)
    return keys


GEMINI_KEYS = _gemini_keys()
OPENROUTER_KEY = _clean(ENV.get("OPENROUTER_API_KEY")) or None


GEMINI_TIMEOUT_MS = int(os.environ.get("LABX_GEMINI_TIMEOUT_MS", "120000"))
VERBOSE = os.environ.get("LABX_VERBOSE") == "1"


class ModelError(Exception):
    pass


def _images(image_bytes):
    """One JPEG, or several of the same form sent together in one request."""
    return list(image_bytes) if isinstance(image_bytes, (list, tuple)) else [image_bytes]


def _cache_path(model, prompt, image_bytes, extra=""):
    h = hashlib.sha256()
    for part in (model, prompt, extra):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    for img in _images(image_bytes):        # a single image hashes exactly as before: the existing cache stays valid
        h.update(hashlib.sha256(img).digest())
    return CACHE_DIR / f"{h.hexdigest()[:32]}.json"


_CLIENTS = {}


def _client(key_index):
    """One client per key for the whole process: a new client per call leaks its connection pool (memory grew
    page after page until the server was killed)."""
    if key_index not in _CLIENTS:
        from google import genai
        from google.genai import types
        _CLIENTS[key_index] = genai.Client(api_key=GEMINI_KEYS[key_index],
                                           http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))
    return _CLIENTS[key_index]


def _gemini_call(model, prompt, image_bytes, schema, key_index):
    from google.genai import types

    client = _client(key_index)
    cfg = dict(response_mime_type="application/json", temperature=0)
    if schema:
        cfg["response_json_schema"] = schema
    resp = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=img, mime_type="image/jpeg") for img in _images(image_bytes)] + [prompt],
        config=types.GenerateContentConfig(**cfg),
    )
    return resp.text


def _openrouter_call(model, prompt, image_bytes, schema):
    images = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(img).decode()}"}}
              for img in _images(image_bytes)]
    body = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": images + [{"type": "text", "text": prompt}]}],
    }
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}}
    r = requests.post("https://openrouter.ai/api/v1/chat/completions", json=body, timeout=180,
                      headers={"Authorization": f"Bearer {OPENROUTER_KEY}"})
    if r.status_code != 200:
        raise ModelError(f"openrouter {r.status_code}: {r.text[:300]}")
    data = r.json()
    if "choices" not in data:
        raise ModelError(f"openrouter: {str(data)[:300]}")
    return data["choices"][0]["message"]["content"]


def _parse_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):]
    s, e = t.find("{"), t.rfind("}")
    if s < 0 or e < 0:
        raise ModelError(f"no JSON in response: {t[:200]}")
    return json.loads(t[s:e + 1])


DEAD_FILE = CACHE_ROOT / "quota_dead.json"


def _quota_day():
    """Free-tier daily quotas reset at midnight Pacific time."""
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)).date().isoformat()


def _load_dead():
    try:
        d = json.loads(DEAD_FILE.read_text(encoding="utf-8"))
        return set(map(tuple, d["dead"])) if d.get("day") == _quota_day() else set()
    except Exception:
        return set()


def _fp(key_index):
    """Short fingerprint of a key (never the key itself) - stable even if keys are added or reordered."""
    return hashlib.sha256(GEMINI_KEYS[key_index].encode()).hexdigest()[:8]


_DEAD = _load_dead()  # {(model_name, key fingerprint)} whose daily quota is used up


def _mark_dead(name, key_index):
    _DEAD.add((name, _fp(key_index)))
    try:
        DEAD_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEAD_FILE.write_text(json.dumps({"day": _quota_day(), "dead": sorted(_DEAD)}), encoding="utf-8")
    except OSError:
        pass


def cached(model, prompt, image_bytes, schema=None):
    cp = _cache_path(model, prompt, image_bytes, json.dumps(schema, sort_keys=True) if schema else "")
    return json.loads(cp.read_text(encoding="utf-8"))["parsed"] if cp.exists() else None


def ask(model, prompt, image_bytes, schema=None, retries=4, use_cache=True):
    """Send one image + prompt to `model` ("gemini:<name>" or "openrouter:<name>"), return parsed JSON."""
    cp = _cache_path(model, prompt, image_bytes, json.dumps(schema, sort_keys=True) if schema else "")
    if use_cache and cp.exists():
        return json.loads(cp.read_text(encoding="utf-8"))["parsed"]
    if os.environ.get("LABX_OFFLINE") == "1":
        raise ModelError("offline: not cached")
    provider, name = model.split(":", 1)
    last = None
    for attempt in range(retries):
        try:
            if provider == "gemini":
                live = [i for i in range(len(GEMINI_KEYS)) if (name, _fp(i)) not in _DEAD]
                if not live:
                    raise ModelError(f"429 daily quota used up on every key for {name}")
                key_index = live[attempt % len(live)]
                try:
                    raw = _gemini_call(name, prompt, image_bytes, schema, key_index)
                except Exception as e:
                    if "429" in str(e) and "PerDay" in str(e):
                        _mark_dead(name, key_index)
                    raise
            elif provider == "openrouter":
                raw = _openrouter_call(name, prompt, image_bytes, schema)
            else:
                raise ValueError(provider)
            parsed = _parse_json(raw)
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps({"model": model, "raw": raw, "parsed": parsed}, ensure_ascii=False),
                              encoding="utf-8")
            except OSError:
                pass  # a read-only host must not lose a good answer
            return parsed
        except Exception as e:  # rate limits / overload / bad JSON: back off, rotate key, retry
            last = e
            msg = str(e)
            if VERBOSE:
                print(f"    [{model} attempt {attempt + 1}] {msg[:120]}", file=sys.stderr, flush=True)
            if "404" in msg or "NOT_FOUND" in msg or "used up on every key" in msg:
                break
            if "429" in msg and "PerDay" in msg:
                continue  # next key right away
            time.sleep(min(30, 4 * 2 ** attempt))
    raise ModelError(f"{model}: {str(last)[:300]}")


SLOT_BUDGET_S = float(os.environ.get("LABX_SLOT_BUDGET_S", "0"))   # 0 = no limit (local evaluation)


def ask_slot(slot, prompt, image_bytes, schema=None):
    """A reader slot = ordered list of interchangeable models. Reuse any cached answer first (saves quota),
    else try the models in order. Returns (model_used, parsed).
    LABX_SLOT_BUDGET_S (the server): no further model is started once the slot has used that many seconds, and
    each model gets one attempt - a page must finish inside the web request even when Gemini is overloaded; an
    unanswered slot only leaves its fields for review."""
    for m in slot:
        c = cached(m, prompt, image_bytes, schema)
        if c is not None:
            return m, c
    last, t0 = None, time.monotonic()
    for m in slot:
        if SLOT_BUDGET_S and time.monotonic() - t0 > SLOT_BUDGET_S:
            last = ModelError(f"slot time budget of {SLOT_BUDGET_S:.0f}s used")
            break
        try:
            retries = 1 if SLOT_BUDGET_S else (2 if m != slot[-1] else 4)
            return m, ask(m, prompt, image_bytes, schema=schema, retries=retries)
        except Exception as e:
            last = e
    raise ModelError(f"slot {slot[0]}...: {str(last)[:200]}")
