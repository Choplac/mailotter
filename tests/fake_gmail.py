"""A tiny in-memory stand-in for the googleapiclient Gmail service.

Query support is deliberately simple: 'from:X' matches the sender, other words
match the subject, 'in:inbox' requires the INBOX label. Trashed mail is hidden
from searches, like Gmail.

A message may carry "attachments": [{"filename", "data": bytes, "inline": bool}]
and "internalDate" (milliseconds since 1970, as Gmail sends it). Every API call
is recorded by name in `api`.
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
        self.api = []                      # every API call, by name

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
        body = _b64(m.get("body", "hello").encode())
        payload = {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": m["from"]},
                {"name": "Subject", "value": m["subject"]},
                {"name": "Date", "value": "Sat, 7 Nov 2026 10:00:00 -0800"},
                {"name": "To", "value": "me@example.com"},
            ],
            "body": {"data": body} if fmt == "full" else {},
        }
        if fmt == "full" and m.get("attachments"):
            text = {"mimeType": "text/plain", "body": payload.pop("body")}
            payload.update(mimeType="multipart/mixed", body={}, parts=[text])
            for i, a in enumerate(m["attachments"]):
                size = a.get("size", len(a["data"]))
                part_body = {"size": size, "data": _b64(a["data"])} if a.get("inline") else {"size": size, "attachmentId": f"att{i}"}
                payload["parts"].append({"mimeType": a.get("mime", "application/octet-stream"),
                                         "filename": a["filename"], "body": part_body})
        out = {"id": mid, "snippet": m["subject"], "labelIds": sorted(m["labels"]), "payload": payload}
        if "internalDate" in m:
            out["internalDate"] = m["internalDate"]
        return out

    def _attachment(self, mid, aid):
        data = self.store[mid]["attachments"][int(aid[3:])]["data"]
        return {"size": len(data), "data": _b64(data)}

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

    def _req(self, name, fn):
        def run():
            self.api.append(name)
            return fn()

        return _Req(run)

    def list(self, userId, q, maxResults, pageToken=None):
        return self._req("list", lambda: self._list(q, maxResults, pageToken))

    def get(self, userId, id, format="full", metadataHeaders=None):
        return self._req("get", lambda: self._get(id, format))

    def attachments(self):
        return _Attachments(self)

    def trash(self, userId, id):
        return self._req("trash", lambda: self._label(id, add={"TRASH"}, remove={"INBOX"}, op="trash"))

    def untrash(self, userId, id):
        return self._req("untrash", lambda: self._label(id, remove={"TRASH"}, add={"INBOX"}, op="untrash"))

    def batchModify(self, userId, body):
        def run():
            self.api.append("batchModify")
            self.calls.append(("batchModify", tuple(body["ids"])))
            for mid in body["ids"]:
                m = self.store[mid]
                m["labels"] |= set(body.get("addLabelIds", []))
                m["labels"] -= set(body.get("removeLabelIds", []))
            return {}

        return _Req(run)

    def new_batch_http_request(self):
        return _Batch()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class _Attachments:
    def __init__(self, gmail):
        self.gmail = gmail

    def get(self, userId, messageId, id):
        return self.gmail._req("attachments.get", lambda: self.gmail._attachment(messageId, id))


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
