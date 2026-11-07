"""Gmail operations for one account. Takes any object shaped like the
googleapiclient Gmail service, so tests can pass a fake."""

from __future__ import annotations

import base64
import time
from collections import Counter
from email.utils import parseaddr
from html import unescape
import re

GET_BATCH = 50          # Gmail recommends at most 50 calls per batch
TRASH_BATCH = 50
MODIFY_CHUNK = 1000     # batchModify limit
PAUSE = 1.0             # seconds between batches, keeps us under per-user quota
BODY_LIMIT = 20_000


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _headers(msg: dict) -> dict:
    out = {}
    for h in (msg.get("payload") or {}).get("headers") or []:
        out.setdefault(h.get("name", "").lower(), h.get("value", ""))
    return out


def _summary(alias: str, msg: dict) -> dict:
    h = _headers(msg)
    return {
        "account": alias,
        "id": msg.get("id"),
        "from": h.get("from", ""),
        "subject": h.get("subject", ""),
        "date": h.get("date", ""),
        "snippet": unescape(msg.get("snippet", "")),
        "labels": msg.get("labelIds", []),
    }


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _body(payload: dict) -> tuple[str, list[str]]:
    """Plain-text body (falls back to stripped HTML) and attachment names."""
    plain, html, files = [], [], []

    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        if part.get("filename"):
            files.append(part["filename"])
        elif body.get("data") and mime == "text/plain":
            plain.append(_decode(body["data"]))
        elif body.get("data") and mime == "text/html":
            html.append(_decode(body["data"]))
        for p in part.get("parts") or []:
            walk(p)

    walk(payload or {})
    if plain:
        text = "\n".join(plain)
    else:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", "\n".join(html), flags=re.S | re.I)
        text = unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"[ \t]+", " ", text)
    return text.strip(), files


class Mailbox:
    def __init__(self, alias: str, service, pause: float = PAUSE):
        self.alias = alias
        self.svc = service
        self.pause = pause

    @property
    def _msgs(self):
        return self.svc.users().messages()

    # ---- finding ----------------------------------------------------------

    def ids(self, query: str, cap: int) -> tuple[list[str], bool]:
        """Message ids matching query, up to cap. Second value: more exist."""
        out, token = [], None
        while True:
            resp = self._msgs.list(
                userId="me", q=query, maxResults=min(500, cap + 1 - len(out)), pageToken=token
            ).execute()
            out.extend(m["id"] for m in resp.get("messages", []))
            token = resp.get("nextPageToken")
            if len(out) > cap:
                return out[:cap], True
            if not token:
                return out, False

    def _get_many(self, ids: list[str], fmt: str = "metadata") -> list[dict]:
        found: dict[str, dict] = {}
        for i, chunk in enumerate(_chunks(ids, GET_BATCH)):
            if i and self.pause:
                time.sleep(self.pause)
            batch = self.svc.new_batch_http_request()

            def cb(request_id, response, exception):
                if exception is None and response:
                    found[request_id] = response

            for mid in chunk:
                batch.add(
                    self._msgs.get(
                        userId="me", id=mid, format=fmt, metadataHeaders=["From", "Subject", "Date"]
                    ),
                    callback=cb,
                    request_id=mid,
                )
            batch.execute()
        return [found[m] for m in ids if m in found]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        ids, _ = self.ids(query, limit)
        return [_summary(self.alias, m) for m in self._get_many(ids)]

    def read(self, message_id: str) -> dict:
        msg = self._msgs.get(userId="me", id=message_id, format="full").execute()
        text, files = _body(msg.get("payload"))
        out = _summary(self.alias, msg)
        out.update(
            to=_headers(msg).get("to", ""),
            body=text[:BODY_LIMIT] + ("\n[... cut ...]" if len(text) > BODY_LIMIT else ""),
            attachments=files,
        )
        return out

    def top_senders(self, query: str, sample: int) -> tuple[Counter, int, bool]:
        ids, more = self.ids(query, sample)
        counts: Counter = Counter()
        for msg in self._get_many(ids):
            name, addr = parseaddr(_headers(msg).get("from", ""))
            counts[(addr or name or "(unknown)").lower()] += 1
        return counts, len(ids), more

    # ---- changing (all reversible) ---------------------------------------

    def _each(self, ids: list[str], make) -> tuple[list[str], list[str]]:
        """Run one request per id in batches; retry failures once."""
        done: list[str] = []
        todo = list(ids)
        for attempt in range(2):
            failed: list[str] = []
            for i, chunk in enumerate(_chunks(todo, TRASH_BATCH)):
                if (i or attempt) and self.pause:
                    time.sleep(self.pause * (2 if attempt else 1))
                batch = self.svc.new_batch_http_request()

                def cb(request_id, response, exception):
                    (failed if exception is not None else done).append(request_id)

                for mid in chunk:
                    batch.add(make(mid), callback=cb, request_id=mid)
                batch.execute()
            todo = failed
            if not todo:
                break
        return done, todo

    def trash(self, ids: list[str]):
        return self._each(ids, lambda mid: self._msgs.trash(userId="me", id=mid))

    def untrash(self, ids: list[str]):
        return self._each(ids, lambda mid: self._msgs.untrash(userId="me", id=mid))

    def _relabel(self, ids: list[str], add: list[str], remove: list[str]):
        for chunk in _chunks(ids, MODIFY_CHUNK):
            self._msgs.batchModify(
                userId="me", body={"ids": chunk, "addLabelIds": add, "removeLabelIds": remove}
            ).execute()
        return list(ids), []

    def archive(self, ids: list[str]):
        return self._relabel(ids, [], ["INBOX"])

    def unarchive(self, ids: list[str]):
        return self._relabel(ids, ["INBOX"], [])
