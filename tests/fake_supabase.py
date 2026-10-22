"""In-memory stand-in for the parts of Supabase PostgREST and Storage that SupabaseRepository uses.

No network. Implements only: eq / is.null filters, select, order, Prefer return=representation /
return=minimal / resolution=ignore-duplicates, and object POST (x-upsert: false) / GET / DELETE.
"""
from __future__ import annotations

import json
import urllib.parse

PRIMARY_KEYS = {
    "lx_documents": ("file_id",),
    "lx_drafts": ("file_id",),
    "lx_sessions": ("token_hash",),
    "lx_workspace_members": ("user_id", "workspace_id"),
}

# Bucket settings as created by migrations/001_labextract_init.sql (Supabase rejects other uploads).
BUCKETS = {
    "labextract-originals": {"allowed_mime_types": {"application/pdf", "image/jpeg", "image/png"},
                             "file_size_limit": 4 * 1024 * 1024},
}


class FakeSupabase:
    def __init__(self, base_url: str = "https://synthetic.supabase.test", key: str = "synthetic-service-key"):
        self.base_url = base_url
        self.key = key
        self.tables: dict[str, list[dict]] = {name: [] for name in PRIMARY_KEYS}
        self.objects: dict[str, bytes] = {}
        self.object_types: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail_next: dict[str, int] = {}     # e.g. {"POST documents": 500}

    # -- helpers
    @staticmethod
    def _matches(row: dict, filters: dict) -> bool:
        for column, condition in filters.items():
            if condition == "is.null":
                if row.get(column) is not None:
                    return False
            elif condition.startswith("eq."):
                if str(row.get(column)) != condition[3:]:
                    return False
            else:
                raise AssertionError(f"unsupported filter {condition}")
        return True

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None):
        assert headers.get("apikey") == self.key and headers.get("Authorization") == f"Bearer {self.key}"
        parsed = urllib.parse.urlsplit(url)
        assert self.key not in url
        self.requests.append((method, parsed.path))
        if parsed.path.startswith("/rest/v1/"):
            return self._rest(method, parsed.path[len("/rest/v1/"):], urllib.parse.parse_qs(parsed.query),
                              headers, body)
        if parsed.path.startswith("/storage/v1/object/"):
            return self._storage(method, urllib.parse.unquote(parsed.path[len("/storage/v1/object/"):]),
                                 headers, body)
        raise AssertionError(f"unexpected url {url}")

    def _rest(self, method, table, query, headers, body):
        failure = self.fail_next.pop(f"{method} {table}", None)
        if failure:
            return failure, b"{}"
        params = {k: v[0] for k, v in query.items()}
        select = params.pop("select", None)
        order = params.pop("order", None)
        prefer = headers.get("Prefer", "")
        rows = self.tables[table]
        key = PRIMARY_KEYS[table]

        if method == "POST":
            new = json.loads(body)
            for item in new if isinstance(new, list) else [new]:
                if any(all(r[k] == item[k] for k in key) for r in rows):
                    if "ignore-duplicates" in prefer:
                        continue
                    return 409, b'{"code":"23505"}'
                rows.append(dict(item, created_at=item.get("created_at", f"t{len(rows):06d}")))
            return 201, b""
        selected = [r for r in rows if self._matches(r, params)]
        if method == "GET":
            if order:
                column, _, direction = order.partition(".")
                selected.sort(key=lambda r: r.get(column) or "", reverse=direction == "desc")
            if select:
                columns = select.split(",")
                selected = [{c: r.get(c) for c in columns} for r in selected]
            return 200, json.dumps(selected).encode()
        if method == "PATCH":
            update = json.loads(body)
            for r in selected:
                r.update(update)
            return 200, (json.dumps(selected).encode() if "return=representation" in prefer else b"")
        raise AssertionError(f"unexpected {method}")

    def _storage(self, method, path, headers, body):
        if method == "POST":
            bucket = BUCKETS[path.split("/", 1)[0]]
            if headers.get("Content-Type") not in bucket["allowed_mime_types"]:
                return 415, b'{"error":"invalid_mime_type"}'
            if len(body) > bucket["file_size_limit"]:
                return 413, b'{"error":"Payload too large"}'
            if path in self.objects and headers.get("x-upsert") == "false":
                return 409, b'{"error":"Duplicate"}'
            self.objects[path] = body
            self.object_types[path] = headers["Content-Type"]
            return 200, b"{}"
        if method == "GET":
            return (200, self.objects[path]) if path in self.objects else (404, b"{}")
        if method == "DELETE":
            self.objects.pop(path, None)
            self.object_types.pop(path, None)
            return 200, b"{}"
        raise AssertionError(f"unexpected {method}")
