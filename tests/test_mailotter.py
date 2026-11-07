import os
import stat

import pytest

from mailotter import actions, store
from mailotter.core import read_one, search_all, senders_report
from mailotter.mailbox import Mailbox

from fake_gmail import FakeGmail


def msgs(n, sender, subject="promo", start=0, labels=("INBOX",)):
    return [{"id": f"{sender[:3]}{i}", "from": sender, "subject": f"{subject} {i}", "labels": list(labels)}
            for i in range(start, start + n)]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Two accounts with fake mailboxes, isolated config dir, no pauses."""
    monkeypatch.setenv("MAILOTTER_HOME", str(tmp_path / "home"))
    store.save_account("personal", "me@gmail.com")
    store.save_account("old", "old@gmail.com")
    fakes = {
        "personal": FakeGmail(msgs(30, "news@shop.com") + msgs(3, "mom@family.com", "dinner")),
        "old": FakeGmail(msgs(12, "news@shop.com", start=100) + msgs(2, "bank@bank.com", "statement")),
    }
    factory = lambda alias: Mailbox(alias, fakes[alias], pause=0)
    return fakes, factory


def in_trash(fake):
    return {mid for mid, m in fake.store.items() if "TRASH" in m["labels"]}


def test_files_are_private(world):
    home = store.home()
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home / "accounts.json").st_mode) == 0o600


def test_search_across_all_accounts(world):
    _, factory = world
    res = search_all("all", "from:news@shop.com", 50, factory)
    assert res["count"] == 42
    assert {r["account"] for r in res["results"]} == {"personal", "old"}


def test_resolve_by_email_and_unknown(world):
    assert store.resolve("old@gmail.com") == ["old"]
    with pytest.raises(ValueError):
        store.resolve("nobody")


def test_read_one_plain_text(world):
    _, factory = world
    m = read_one("personal", "mom0", factory)
    assert m["from"] == "mom@family.com" and m["body"] == "hello"


def test_top_senders(world):
    _, factory = world
    res = senders_report("all", "in:inbox", 500, 5, factory)
    assert res["top"][0] == {"sender": "news@shop.com", "count": 42}
    assert res["scanned"] == {"old": 14, "personal": 33}


def test_preview_changes_nothing(world):
    fakes, factory = world
    p = actions.preview("trash", "all", "from:news@shop.com", factory)
    assert p["count"] == 42 and p["per_account"] == {"personal": 30, "old": 12}
    assert not in_trash(fakes["personal"]) and not in_trash(fakes["old"])
    assert all(op != "trash" for op, _ in fakes["personal"].calls)


def test_confirm_trashes_exactly_the_previewed_mail(world):
    fakes, factory = world
    p = actions.preview("trash", "personal", "from:news@shop.com", factory)
    # new junk arrives after the preview: must NOT be trashed
    fakes["personal"].store["late"] = {"id": "late", "from": "news@shop.com", "subject": "new", "labels": {"INBOX"}}
    r = actions.confirm(p["code"], factory)
    assert r["done"] == 30 and r["failed"] == 0
    assert "late" not in in_trash(fakes["personal"])
    assert len(in_trash(fakes["personal"])) == 30
    assert not in_trash(fakes["old"])          # other account untouched
    assert "mom0" not in in_trash(fakes["personal"])


def test_code_is_single_use_and_expires(world, monkeypatch):
    _, factory = world
    p = actions.preview("trash", "all", "from:bank@bank.com", factory)
    actions.confirm(p["code"], factory)
    with pytest.raises(ValueError):
        actions.confirm(p["code"], factory)
    p2 = actions.preview("trash", "all", "from:mom@family.com", factory)
    real = actions.time.time
    monkeypatch.setattr(actions.time, "time", lambda: real() + actions.PREVIEW_TTL + 1)
    with pytest.raises(ValueError):
        actions.confirm(p2["code"], factory)


def test_over_limit_refuses_whole_job(world, monkeypatch):
    fakes, factory = world
    monkeypatch.setenv("MAILOTTER_MAX_BATCH", "20")
    with pytest.raises(ValueError, match="Nothing was changed"):
        actions.preview("trash", "all", "from:news@shop.com", factory)
    assert not in_trash(fakes["personal"])


def test_total_over_limit_across_accounts_refuses(world, monkeypatch):
    _, factory = world
    monkeypatch.setenv("MAILOTTER_MAX_BATCH", "35")  # 30 + 12 = 42 > 35, each account alone fits
    with pytest.raises(ValueError, match="across accounts"):
        actions.preview("trash", "all", "from:news@shop.com", factory)


def test_empty_query_refused(world):
    _, factory = world
    with pytest.raises(ValueError):
        actions.preview("trash", "all", "   ", factory)


def test_undo_restores_everything(world):
    fakes, factory = world
    p = actions.preview("trash", "all", "from:news@shop.com", factory)
    r = actions.confirm(p["code"], factory)
    u = actions.undo("last", factory)
    assert u["undid"] == r["id"] and u["restored"] == 42
    assert not in_trash(fakes["personal"]) and not in_trash(fakes["old"])
    assert actions.history()[0]["undone"] is True
    with pytest.raises(ValueError):
        actions.undo(r["id"], factory)


def test_archive_only_touches_inbox_and_undo_puts_back(world):
    fakes, factory = world
    fakes["personal"].store["news@0"] = {"id": "news@0", "from": "news@shop.com", "subject": "promo old",
                                          "labels": {"CATEGORY_PROMOTIONS"}}  # already archived
    p = actions.preview("archive", "personal", "from:news@shop.com", factory)
    assert p["count"] == 30
    actions.confirm(p["code"], factory)
    assert all("INBOX" not in fakes["personal"].store[f"new{i}"]["labels"] for i in range(30))
    actions.undo("last", factory)
    assert all("INBOX" in fakes["personal"].store[f"new{i}"]["labels"] for i in range(30))
    assert "INBOX" not in fakes["personal"].store["news@0"]["labels"]


def test_failures_retried_once_then_reported(world):
    fakes, factory = world
    fakes["personal"].fail_once = {"new1"}
    fakes["personal"].fail_ids = {"new2"}
    p = actions.preview("trash", "personal", "from:news@shop.com", factory)
    r = actions.confirm(p["code"], factory)
    assert r["done"] == 29 and r["failed"] == 1
    assert "new1" in in_trash(fakes["personal"]) and "new2" not in in_trash(fakes["personal"])


def test_mailbox_has_no_send_or_delete():
    for name in ("send", "delete", "batch_delete", "permanent_delete"):
        assert not hasattr(Mailbox, name)


def test_scope_is_modify_only():
    from mailotter.auth import SCOPES

    assert SCOPES == ["https://www.googleapis.com/auth/gmail.modify"]
