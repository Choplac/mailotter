"""A tiny in-memory stand-in for the googleapiclient Gmail service.

Query support is deliberately simple: 'from:X' matches the sender, other words
match the subject, 'in:inbox' requires the INBOX label. Trashed mail is hidden
from searches, like Gmail.
"""

from __future__ import annotations

import base64


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeGmail:
    def __init__(self, messages, fail_ids=(), fail_once=()):
        # messages: list of dicts with id, from, subject, labels
        self.store = {m["id"]: {**m, "labels": set(m.get("labels", ["INBOX"]))} for m in messages}
        self.fail_ids = set(fail_ids)      # always fail on trash/untrash
        self.fail_once = set(fail_once)    # fail first time only
        self.calls = []

    # ---- query --------------------------------------------------------------
    def _match(self, msg, q):
        if "TRASH" in msg["labels"]:
            return False
        for raw in q.replace("(", " ").replace(")", " ").split():
            if raw.startswith("from:"):
                if raw[5:].lower() not in msg["from"].lower():
                    return False
            elif raw == "in:inbox":
                if "INBOX" not in msg["labels"]:
                    return False
            elif raw.lower() not in msg["subject"].lower():
                return False
        return True

    def _list(self, q, maxResults, pageToken):
        hits = [mid for mid, m in self.store.items() if self._match(m, q)]
        start = int(pageToken or 0)
        page = hits[start : start + maxResults]
        out = {"messages": [{"id": i} for i in page]}
        if start + maxResults < len(hits):
            out["nextPageToken"] = str(start + maxResults)
        return out

    def _get(self, mid, fmt):
        m = self.store[mid]
        body = base64.urlsafe_b64encode(m.get("body", "hello").encode()).decode().rstrip("=")
        return {
            "id": mid,
            "snippet": m["subject"],
            "labelIds": sorted(m["labels"]),
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": m["from"]},
                    {"name": "Subject", "value": m["subject"]},
                    {"name": "Date", "value": "Sat, 7 Nov 2026 10:00:00 -0800"},
                    {"name": "To", "value": "me@example.com"},
                ],
                "body": {"data": body} if fmt == "full" else {},
            },
        }

    def _label(self, mid, add=(), remove=(), op=""):
        self.calls.append((op, mid))
        if mid in self.fail_ids:
            raise RuntimeError("boom")
        if mid in self.fail_once:
            self.fail_once.discard(mid)
            raise RuntimeError("rate limited")
        m = self.store[mid]
        m["labels"] |= set(add)
        m["labels"] -= set(remove)
        return {"id": mid}

    # ---- googleapiclient surface -----------------------------------------
    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults, pageToken=None):
        return _Req(lambda: self._list(q, maxResults, pageToken))

    def get(self, userId, id, format="full", metadataHeaders=None):
        return _Req(lambda: self._get(id, format))

    def trash(self, userId, id):
        return _Req(lambda: self._label(id, add={"TRASH"}, remove={"INBOX"}, op="trash"))

    def untrash(self, userId, id):
        return _Req(lambda: self._label(id, remove={"TRASH"}, add={"INBOX"}, op="untrash"))

    def batchModify(self, userId, body):
        def run():
            self.calls.append(("batchModify", tuple(body["ids"])))
            for mid in body["ids"]:
                m = self.store[mid]
                m["labels"] |= set(body.get("addLabelIds", []))
                m["labels"] -= set(body.get("removeLabelIds", []))
            return {}

        return _Req(run)

    def new_batch_http_request(self):
        return _Batch()


class _Batch:
    def __init__(self):
        self.items = []

    def add(self, req, callback, request_id):
        self.items.append((req, callback, request_id))

    def execute(self):
        for req, cb, rid in self.items:
            try:
                cb(rid, req.execute(), None)
            except Exception as exc:
                cb(rid, None, exc)
