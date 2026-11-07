"""Read-only helpers shared by the CLI and the MCP server."""

from __future__ import annotations

from collections import Counter

from . import store
from .actions import MailboxFactory, default_factory


def search_all(account: str, query: str, limit: int = 20, factory: MailboxFactory = default_factory) -> dict:
    results, errors, unreadable = [], {}, 0
    for alias in store.resolve(account):
        try:
            found, bad = factory(alias).search(query, limit)
        except Exception as exc:  # one broken account shouldn't hide the others
            errors[alias] = str(exc)
            continue
        results.extend(found)
        unreadable += bad
    out = {"count": len(results), "results": results}
    if unreadable:
        out["note"] = f"{unreadable} matching emails could not be loaded (Gmail busy). Try again shortly."
    if errors:
        out["errors"] = errors
    return out


def read_one(account: str, message_id: str, factory: MailboxFactory = default_factory) -> dict:
    aliases = store.resolve(account)
    if len(aliases) != 1:
        raise ValueError("Name one account to read from.")
    return factory(aliases[0]).read(message_id)


def senders_report(
    account: str, query: str, sample: int, show: int, factory: MailboxFactory = default_factory
) -> dict:
    total: Counter = Counter()
    scanned, partial, errors = {}, [], {}
    for alias in store.resolve(account):
        try:
            counts, n, more, bad = factory(alias).top_senders(query, sample)
        except Exception as exc:
            errors[alias] = str(exc)
            continue
        total.update(counts)
        scanned[alias] = n
        if bad:
            errors[alias] = f"{bad} emails could not be loaded (Gmail busy); counts are slightly low"
        if more:
            partial.append(alias)
    out = {
        "scanned": scanned,
        "top": [{"sender": s, "count": c} for s, c in total.most_common(show)],
    }
    if partial:
        out["note"] = f"Only the newest {sample} emails were counted in: {', '.join(partial)}."
    if errors:
        out["errors"] = errors
    return out
