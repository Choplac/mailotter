"""Two-step cleanup: preview -> confirm, with an undo log.

A preview freezes the exact list of messages and returns a short code. Confirm
acts on exactly that list (mail that arrived later is never touched), once,
within 15 minutes. If a query matches more than the limit, the preview refuses
instead of doing part of the job. Every confirmed action is logged so it can be
undone.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections import Counter
from typing import Callable

from . import store
from .mailbox import Mailbox

PREVIEW_TTL = 15 * 60
SAMPLE = 5
UNDO = {"trash": "untrash", "archive": "unarchive"}


def max_batch() -> int:
    return int(os.environ.get("MAILOTTER_MAX_BATCH", "2000"))


MailboxFactory = Callable[[str], Mailbox]


def default_factory(alias: str) -> Mailbox:
    from .auth import service

    return Mailbox(alias, service(alias))


def _pending_path():
    return store.state_dir() / "pending.json"


def _log_path():
    return store.state_dir() / "history.jsonl"


def _load_pending() -> dict:
    now = time.time()
    data = store.read_json(_pending_path(), {})
    return {k: v for k, v in data.items() if now - v["created"] < PREVIEW_TTL}


def _save_pending(data: dict) -> None:
    store.write_private(_pending_path(), json.dumps(data))


def _query_for(action: str, query: str) -> str:
    query = (query or "").strip()
    if not query:
        raise ValueError("A search query is required (an empty query would match everything).")
    # Archive only what is in the inbox, so undo puts back exactly that.
    return f"({query}) in:inbox" if action == "archive" else f"({query})"


def preview(action: str, account: str, query: str, factory: MailboxFactory = default_factory) -> dict:
    if action not in UNDO:
        raise ValueError(f"Unknown action {action!r}")
    q = _query_for(action, query)
    cap = max_batch()
    plans, samples, total = {}, [], 0
    for alias in store.resolve(account):
        box = factory(alias)
        ids, more = box.ids(q, cap)
        if more:
            raise ValueError(
                f"'{alias}' has more than {cap} matching emails. Nothing was changed. "
                "Narrow the search (for example add older_than:1y or a sender) and preview again."
            )
        total += len(ids)
        if total > cap:
            raise ValueError(f"More than {cap} matching emails across accounts. Nothing was changed; narrow the search.")
        if ids:
            plans[alias] = ids
            samples.extend(box.search(q, SAMPLE))
    if not total:
        return {"action": action, "count": 0, "message": "No matching emails. Nothing to do."}
    code = secrets.token_hex(3)
    pending = _load_pending()
    pending[code] = {"action": action, "query": query, "plans": plans, "created": time.time()}
    _save_pending(pending)
    senders = Counter(s["from"] for s in samples)
    return {
        "action": action,
        "code": code,
        "count": total,
        "per_account": {a: len(i) for a, i in plans.items()},
        "examples": [{k: s[k] for k in ("account", "from", "subject", "date")} for s in samples[: SAMPLE * 2]],
        "example_senders": dict(senders.most_common(5)),
        "expires_in_minutes": PREVIEW_TTL // 60,
        "next": f"Show this to the user. Only after they say yes: confirm code {code}.",
    }


def confirm(code: str, factory: MailboxFactory = default_factory) -> dict:
    pending = _load_pending()
    plan = pending.pop(code, None)
    _save_pending(pending)  # single use, even if something below fails
    if not plan:
        raise ValueError("That preview code is unknown, already used, or older than 15 minutes. Preview again.")
    action = plan["action"]
    op = {
        "id": f"op-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}",
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "query": plan["query"],
        "done": {},
        "failed": {},
        "undone": False,
    }
    for alias, ids in plan["plans"].items():
        done, failed = getattr(factory(alias), action)(ids)
        op["done"][alias] = done
        if failed:
            op["failed"][alias] = failed
    _append(op)
    return _report(op)


def _append(op: dict) -> None:
    path = _log_path()
    line = json.dumps(op) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line)


def _history_all() -> list[dict]:
    path = _log_path()
    if not path.exists():
        return []
    ops: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("undo_of"):
                if rec["undo_of"] in ops:
                    ops[rec["undo_of"]]["undone"] = True
            else:
                ops[rec["id"]] = rec
    return list(ops.values())


def _report(op: dict) -> dict:
    return {
        "id": op["id"],
        "time": op["time"],
        "action": op["action"],
        "query": op["query"],
        "done": sum(len(v) for v in op["done"].values()),
        "failed": sum(len(v) for v in op["failed"].values()),
        "per_account": {a: len(v) for a, v in op["done"].items()},
        "undone": op.get("undone", False),
    }


def history(limit: int = 10) -> list[dict]:
    return [_report(op) for op in reversed(_history_all()[-limit:])]


def undo(op_id: str = "last", factory: MailboxFactory = default_factory) -> dict:
    ops = _history_all()
    if op_id == "last":
        live = [o for o in ops if not o["undone"]]
        if not live:
            raise ValueError("Nothing to undo.")
        op = live[-1]
    else:
        op = next((o for o in ops if o["id"] == op_id), None)
        if not op:
            raise ValueError(f"No action with id {op_id}.")
        if op["undone"]:
            raise ValueError(f"{op_id} was already undone.")
    reverse = UNDO[op["action"]]
    restored, failed = {}, {}
    for alias, ids in op["done"].items():
        ok, bad = getattr(factory(alias), reverse)(ids)
        restored[alias] = len(ok)
        if bad:
            failed[alias] = bad
    _append({"undo_of": op["id"], "time": time.strftime("%Y-%m-%d %H:%M:%S"), "failed": failed})
    return {
        "undid": op["id"],
        "action": reverse,
        "restored": sum(restored.values()),
        "failed": sum(len(v) for v in failed.values()),
        "per_account": restored,
    }
