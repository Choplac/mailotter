"""Two-step cleanup: preview -> confirm, with an undo journal.

- A preview freezes the exact list of messages and returns a short code.
- Confirm uses the code once, within 15 minutes, and acts only on previewed
  messages that *still* match (already-trashed or already-archived mail is
  skipped, so undo never reverses someone else's change).
- If a query matches more than the limit, the preview refuses the whole job.
- Every account is connected before anything changes; if one can't be reached,
  nothing changes.
- The journal is written before the first change and after each account, so a
  crash still leaves enough to undo.
"""

from __future__ import annotations

import json
import os
import secrets
import time
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


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


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
    plans, samples, total, unreadable = {}, [], 0, 0
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
            found, bad = box.search(q, SAMPLE)
            samples.extend(found)
            unreadable += bad
    if not total:
        return {"action": action, "count": 0, "message": "No matching emails. Nothing to do."}
    code = secrets.token_hex(4)
    with store.locked():
        pending = _load_pending()
        pending[code] = {"action": action, "query": query, "plans": plans, "created": time.time()}
        _save_pending(pending)
    out = {
        "action": action,
        "code": code,
        "count": total,
        "per_account": {a: len(i) for a, i in plans.items()},
        "examples": [{k: s[k] for k in ("account", "from", "subject", "date")} for s in samples],
        "expires_in_minutes": PREVIEW_TTL // 60,
        "next": f"Show this to the user. Only after they say yes: confirm code {code}.",
    }
    if unreadable:
        out["note"] = f"{unreadable} example emails could not be loaded (Gmail busy); the count is still exact."
    return out


def _append(rec: dict) -> None:
    line = json.dumps(rec) + "\n"
    with store.locked():
        fd = os.open(_log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def confirm(code: str, factory: MailboxFactory = default_factory) -> dict:
    with store.locked():  # consume the code atomically: it can never run twice
        pending = _load_pending()
        plan = pending.pop(code, None)
        _save_pending(pending)
    if not plan:
        raise ValueError("That preview code is unknown, already used, or older than 15 minutes. Preview again.")
    action = plan["action"]
    q = _query_for(action, plan["query"])

    # Connect every account and re-check what still matches before changing anything.
    boxes, targets, skipped = {}, {}, 0
    for alias, frozen in plan["plans"].items():
        try:
            boxes[alias] = box = factory(alias)
            now, _ = box.ids(q, len(frozen) + max_batch() * 5)
        except Exception as exc:
            raise RuntimeError(f"Could not reach '{alias}' ({exc}). Nothing was changed; preview again.") from exc
        live = set(now)
        targets[alias] = [m for m in frozen if m in live]
        skipped += len(frozen) - len(targets[alias])

    op_id = f"op-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    _append({"op": op_id, "time": _now(), "action": action, "query": plan["query"], "targets": targets})
    error = None
    for alias, ids in targets.items():
        if not ids:
            continue
        try:
            done, failed = getattr(boxes[alias], action)(ids)
        except Exception as exc:  # unknown how far it got: keep every id undoable
            _append({"progress": op_id, "alias": alias, "done": [], "failed": [], "uncertain": ids})
            error = f"{alias}: {exc}"
            break  # stop here; report instead of carrying on blind
        _append({"progress": op_id, "alias": alias, "done": done, "failed": failed})
    report = _report(_ops()[op_id])
    report["skipped_already_changed"] = skipped
    if error:
        report["error"] = f"Stopped early ({error}). Whatever changed can be undone with {op_id}."
    return report


def _ops() -> dict[str, dict]:
    """Rebuild every action's state from the journal."""
    path = _log_path()
    ops: dict[str, dict] = {}
    if not path.exists():
        return ops
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if "op" in rec:
            ops[rec["op"]] = {
                "id": rec["op"], "time": rec["time"], "action": rec["action"], "query": rec["query"],
                "targets": rec["targets"], "done": {}, "failed": {}, "restored": {}, "recorded": set(),
            }
        elif "progress" in rec and rec["progress"] in ops:
            op = ops[rec["progress"]]
            a = rec["alias"]
            op["recorded"].add(a)
            # archive is safe to reverse even when unsure; trash only what Gmail confirmed
            undoable = rec["done"] + rec.get("uncertain", []) + (rec["failed"] if op["action"] == "archive" else [])
            op["done"][a] = undoable
            if rec["failed"]:
                op["failed"][a] = rec["failed"]
        elif "undo_of" in rec and rec["undo_of"] in ops:
            r = ops[rec["undo_of"]]["restored"]
            for a, ids in rec["restored"].items():
                r.setdefault(a, set()).update(ids)
    for op in ops.values():
        # crashed before an account's progress was written: assume it may have changed
        for a, ids in op["targets"].items():
            if a not in op["recorded"] and ids:
                op["done"][a] = list(ids)
    return ops


def _remaining(op: dict) -> dict[str, list[str]]:
    out = {}
    for a, ids in op["done"].items():
        left = [m for m in ids if m not in op["restored"].get(a, set())]
        if left:
            out[a] = left
    return out


def _report(op: dict) -> dict:
    remaining = _remaining(op)
    restored_any = any(op["restored"].values())
    return {
        "id": op["id"],
        "time": op["time"],
        "action": op["action"],
        "query": op["query"],
        "done": sum(len(v) for v in op["done"].values()),
        "failed": sum(len(v) for v in op["failed"].values()),
        "per_account": {a: len(v) for a, v in op["done"].items()},
        "undone": restored_any and not remaining,
        "partly_undone": restored_any and bool(remaining),
    }


def history(limit: int = 10) -> list[dict]:
    ops = list(_ops().values())
    return [_report(op) for op in reversed(ops[-limit:])]


def undo(op_id: str = "last", factory: MailboxFactory = default_factory) -> dict:
    ops = _ops()
    if op_id == "last":
        live = [o for o in ops.values() if _remaining(o)]
        if not live:
            raise ValueError("Nothing to undo.")
        op = live[-1]
    else:
        op = ops.get(op_id)
        if not op:
            raise ValueError(f"No action with id {op_id}.")
        if not _remaining(op):
            raise ValueError(f"{op_id} is already fully undone.")
    reverse = UNDO[op["action"]]
    restored, failed = {}, {}
    for alias, ids in _remaining(op).items():
        try:
            ok, bad = getattr(factory(alias), reverse)(ids)
        except Exception as exc:
            ok, bad = [], ids
            failed[alias + " (error)"] = [str(exc)]
        restored[alias] = ok
        if bad:
            failed[alias] = bad
    _append({"undo_of": op["id"], "time": _now(), "restored": restored})
    left = sum(len(v) for v in _remaining(_ops()[op["id"]]).values())
    out = {
        "undid": op["id"],
        "action": reverse,
        "restored": sum(len(v) for v in restored.values()),
        "still_to_restore": left,
        "per_account": {a: len(v) for a, v in restored.items()},
    }
    if left:
        out["next"] = f"Some emails could not be restored yet. Run undo {op['id']} again later."
    return out
