"""Gmail operations for one account. Takes any object shaped like the
googleapiclient Gmail service, so tests can pass a fake."""

from __future__ import annotations

import base64
import re
import time
from collections import Counter
from email.utils import parseaddr
from html import unescape

BATCH = 50              # Gmail recommends at most 50 calls per batch
MODIFY_CHUNK = 1000     # batchModify limit
BODY_LIMIT = 20_000
ATTACHMENT_LIMIT = 25 * 1024 * 1024

# Gmail quota units per call (developers.google.com/workspace/gmail/api/reference/quota)
COST = {"list": 5, "get": 20, "get_full": 60, "attachment": 5, "trash": 20, "untrash": 5, "batchModify": 50}
# Google allows 6,000 units per user per minute, but pushes back well before that in
# practice (a 1,500-email trash at 90 units/s saw ~18% "slow down" errors). Stay at half.
UNITS_PER_SECOND = 50.0
RETRY_WAITS = (30.0, 90.0, 180.0)
_NEXT_FREE: dict[str, float] = {}  # per account: earliest start of the next request (this process)  # seconds before retrying failed requests (quota recovers per minute)


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


def _bytes(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode(data: str) -> str:
    return _bytes(data).decode("utf-8", "replace")


def _parts(part: dict):
    """Every MIME part of a message payload, depth first."""
    yield part
    for p in part.get("parts") or []:
        yield from _parts(p)


def _is_attachment(part: dict) -> bool:
    """A part with a filename, an attachment disposition, or separately stored
    content that is not a text body. Gmail may omit the filename of a real attachment."""
    if part.get("filename"):
        return True
    for h in part.get("headers") or []:
        if h.get("name", "").lower() == "content-disposition":
            if h.get("value", "").strip().lower().startswith("attachment"):
                return True  # even a container: _files then reports it as failed
    mime = (part.get("mimeType") or "").lower()
    if mime.startswith("multipart/"):
        return False
    return bool((part.get("body") or {}).get("attachmentId")) and mime not in ("text/plain", "text/html")


def _body(payload: dict) -> tuple[str, list[str]]:
    """Plain-text body (falls back to stripped HTML) and attachment names."""
    plain, html, files = [], [], []
    for part in _parts(payload or {}):
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        if part.get("filename"):
            files.append(part["filename"])
        elif body.get("data") and mime == "text/plain":
            plain.append(_decode(body["data"]))
        elif body.get("data") and mime == "text/html":
            html.append(_decode(body["data"]))
    if plain:
        text = "\n".join(plain)
    else:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", "\n".join(html), flags=re.S | re.I)
        text = unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"[ \t]+", " ", text)
    return text.strip(), files


class Mailbox:
    def __init__(self, alias: str, service, throttle: bool = True):
        self.alias = alias
        self.svc = service
        self.throttle = throttle

    @property
    def _msgs(self):
        return self.svc.users().messages()

    def _spend(self, units: float) -> None:
        """Pace requests so this account stays under Gmail's per-user quota."""
        if not self.throttle:
            return
        now = time.monotonic()
        nxt = _NEXT_FREE.get(self.alias, 0.0)
        if nxt > now:
            time.sleep(nxt - now)
            now = nxt
        _NEXT_FREE[self.alias] = now + units / UNITS_PER_SECOND

    def _wait(self, seconds: float) -> None:
        if self.throttle:
            time.sleep(seconds)

    def _batched(self, ids: list[str], make, cost: int) -> tuple[dict, list[str]]:
        """One request per id, in paced batches, retrying failures after waits.
        Returns ({id: response}, [ids that still failed])."""
        ok: dict[str, dict] = {}
        todo = list(ids)
        for attempt in range(len(RETRY_WAITS) + 1):
            if attempt:
                self._wait(RETRY_WAITS[attempt - 1])
            failed: list[str] = []
            for chunk in _chunks(todo, BATCH):
                self._spend(cost * len(chunk))
                batch = self.svc.new_batch_http_request()

                def cb(request_id, response, exception):
                    if exception is None:
                        ok[request_id] = response
                    else:
                        failed.append(request_id)

                for mid in chunk:
                    batch.add(make(mid), callback=cb, request_id=mid)
                batch.execute()
            todo = failed
            if not todo:
                break
        return ok, todo

    # ---- finding ----------------------------------------------------------

    def ids(self, query: str, cap: int) -> tuple[list[str], bool]:
        """Message ids matching query, up to cap. Second value: more exist."""
        out, token = [], None
        while True:
            self._spend(COST["list"])
            resp = self._msgs.list(
                userId="me", q=query, maxResults=min(500, cap + 1 - len(out)), pageToken=token
            ).execute()
            out.extend(m["id"] for m in resp.get("messages", []))
            token = resp.get("nextPageToken")
            if len(out) > cap:
                return out[:cap], True
            if not token:
                return out, False

    def _get_many(self, ids: list[str]) -> tuple[list[dict], int]:
        """Metadata for ids, in order. Second value: how many could not be read."""
        found, failed = self._batched(
            ids,
            lambda mid: self._msgs.get(
                userId="me", id=mid, format="metadata", metadataHeaders=["From", "Subject", "Date"]
            ),
            COST["get"],
        )
        return [found[m] for m in ids if m in found], len(failed)

    def search(self, query: str, limit: int = 20) -> tuple[list[dict], int]:
        ids, _ = self.ids(query, limit)
        msgs, unreadable = self._get_many(ids)
        return [_summary(self.alias, m) for m in msgs], unreadable

    def read(self, message_id: str) -> dict:
        self._spend(COST["get_full"])
        msg = self._msgs.get(userId="me", id=message_id, format="full").execute()
        text, files = _body(msg.get("payload"))
        out = _summary(self.alias, msg)
        out.update(
            to=_headers(msg).get("to", ""),
            body=text[:BODY_LIMIT] + ("\n[... cut ...]" if len(text) > BODY_LIMIT else ""),
            attachments=files,
        )
        return out

    def attachments(self, ids: list[str]):
        """Attachment bytes, one message at a time (fetched in batches of BATCH, so
        memory stays bounded). Yields, in order, either {"id", "unreadable": True} or
        {"id", "internal_date" (ms), "files": [{"filename", "mime", "size", "data": bytes | None, "error"}]}.
        `files` lists every attachment part in MIME order, so a part keeps its position
        whether or not its download worked. Parts over ATTACHMENT_LIMIT are not
        downloaded (data None, error "too large")."""
        for chunk in _chunks(ids, BATCH):
            found, _ = self._batched(
                chunk, lambda mid: self._msgs.get(userId="me", id=mid, format="full"), COST["get_full"]
            )
            for mid in chunk:
                msg = found.pop(mid, None)
                yield {"id": mid, "unreadable": True} if msg is None else self._files(mid, msg)

    def _files(self, mid: str, msg: dict) -> dict:
        files, fetch = [], {}
        for part in _parts(msg.get("payload") or {}):
            if not _is_attachment(part):
                continue
            body = part.get("body") or {}
            f = {"filename": part.get("filename") or "", "mime": (part.get("mimeType") or "").lower(),
                 "size": int(body.get("size") or 0), "data": None, "error": None}
            files.append(f)
            if f["mime"].startswith("multipart/"):
                f["error"] = "no data"  # a container marked as an attachment has no content of its own
            elif f["size"] > ATTACHMENT_LIMIT:
                f["error"] = "too large"
            elif body.get("attachmentId"):  # the content lives there, even if `data` is also present (empty)
                fetch[str(len(files) - 1)] = body["attachmentId"]
            elif body.get("data") is not None:
                f["data"] = _bytes(body["data"])
            else:
                f["error"] = "no data"
        got, bad = self._batched(
            list(fetch),
            lambda key: self._msgs.attachments().get(userId="me", messageId=mid, id=fetch[key]),
            COST["attachment"],
        )
        for key, resp in got.items():
            files[int(key)]["data"] = _bytes(resp.get("data") or "")
        for key in bad:
            files[int(key)]["error"] = "could not be loaded"
        for f in files:
            if f["data"] is not None:
                f["size"] = len(f["data"])
                if f["size"] > ATTACHMENT_LIMIT:
                    f["data"], f["error"] = None, "too large"
        return {"id": mid, "internal_date": int(msg.get("internalDate") or 0), "files": files}

    def top_senders(self, query: str, sample: int) -> tuple[Counter, int, bool, int]:
        ids, more = self.ids(query, sample)
        msgs, unreadable = self._get_many(ids)
        counts: Counter = Counter()
        for msg in msgs:
            name, addr = parseaddr(_headers(msg).get("from", ""))
            counts[(addr or name or "(unknown)").lower()] += 1
        return counts, len(msgs), more, unreadable

    # ---- changing (all reversible) ---------------------------------------

    def trash(self, ids: list[str]):
        ok, failed = self._batched(ids, lambda mid: self._msgs.trash(userId="me", id=mid), COST["trash"])
        return [m for m in ids if m in ok], failed

    def untrash(self, ids: list[str]):
        ok, failed = self._batched(ids, lambda mid: self._msgs.untrash(userId="me", id=mid), COST["untrash"])
        return [m for m in ids if m in ok], failed

    def _relabel(self, ids: list[str], add: list[str], remove: list[str]):
        done: list[str] = []
        for chunk in _chunks(ids, MODIFY_CHUNK):
            self._spend(COST["batchModify"])
            try:
                self._msgs.batchModify(
                    userId="me", body={"ids": chunk, "addLabelIds": add, "removeLabelIds": remove}
                ).execute()
            except Exception:
                return done, [m for m in ids if m not in set(done)]
            done.extend(chunk)
        return done, []

    def archive(self, ids: list[str]):
        return self._relabel(ids, [], ["INBOX"])

    def unarchive(self, ids: list[str]):
        return self._relabel(ids, ["INBOX"], [])
