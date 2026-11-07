"""mailotter command line."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import actions, store
from .core import read_one, search_all, senders_report


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_setup(a):
    src = Path(a.client_json).expanduser()
    data = json.loads(src.read_text(encoding="utf-8"))
    if "installed" not in data:
        sys.exit("That file is not a 'Desktop app' OAuth client. In Google Cloud, create client type 'Desktop app'.")
    dest = store.client_secret_path()
    store.write_private(dest, src.read_text(encoding="utf-8"))
    print(f"Saved the Google client key to {dest} (only you can read it).")
    print("You can now delete the downloaded copy. Next: mailotter login <nickname>")


def cmd_login(a):
    from .auth import login

    email = login(a.alias, a.email, a.manual)
    print(f"Signed in '{a.alias}' as {email}.")


def cmd_accounts(a):
    accts = store.accounts()
    if not accts:
        print("No accounts yet. Run: mailotter login <nickname>")
    for alias, email in sorted(accts.items()):
        print(f"{alias:16} {email}")


def cmd_logout(a):
    print("Removed." if store.remove_account(a.alias) else f"No account '{a.alias}'.")
    print("To fully revoke access, also remove MailOtter at https://myaccount.google.com/permissions")


def cmd_search(a):
    res = search_all(a.account, a.query, a.limit)
    if a.json:
        return _print_json(res)
    for m in res["results"]:
        print(f"[{m['account']}] {m['date'][:16]:16}  {m['from'][:40]:40}  {m['subject'][:70]}   ({m['id']})")
    print(f"{res['count']} shown.")
    for alias, err in res.get("errors", {}).items():
        print(f"! {alias}: {err}", file=sys.stderr)


def cmd_read(a):
    m = read_one(a.account, a.message_id)
    print(f"From: {m['from']}\nTo: {m['to']}\nDate: {m['date']}\nSubject: {m['subject']}")
    if m["attachments"]:
        print("Attachments: " + ", ".join(m["attachments"]))
    print("\n" + m["body"])


def cmd_top(a):
    res = senders_report(a.account, a.query, a.sample, a.show)
    if a.json:
        return _print_json(res)
    for row in res["top"]:
        print(f"{row['count']:6}  {row['sender']}")
    print("Scanned: " + ", ".join(f"{k} {v}" for k, v in res["scanned"].items()))
    if res.get("note"):
        print(res["note"])


def _two_step(action, a):
    p = actions.preview(action, a.account, a.query)
    if not p["count"]:
        return print(p["message"])
    verb = "move to Trash" if action == "trash" else "archive (remove from Inbox)"
    print(f"This would {verb} {p['count']} emails: " + ", ".join(f"{k} {v}" for k, v in p["per_account"].items()))
    for e in p["examples"]:
        print(f"  [{e['account']}] {e['from'][:40]:40}  {e['subject'][:60]}")
    if not a.yes and input("Go ahead? Type 'yes': ").strip().lower() != "yes":
        return print("Nothing changed.")
    r = actions.confirm(p["code"])
    print(f"Done: {r['done']} emails ({r['id']}). Failed: {r['failed']}. Skipped (already changed): {r['skipped_already_changed']}.")
    if r.get("error"):
        print(r["error"])
    print(f"Undo with: mailotter undo {r['id']}")


def cmd_trash(a):
    _two_step("trash", a)


def cmd_archive(a):
    _two_step("archive", a)


def cmd_undo(a):
    r = actions.undo(a.op_id)
    print(f"Undid {r['undid']}: restored {r['restored']} emails. Still to restore: {r['still_to_restore']}.")
    if r.get("next"):
        print(r["next"])


def cmd_history(a):
    for op in actions.history(a.limit):
        flag = " (undone)" if op["undone"] else " (partly undone)" if op["partly_undone"] else ""
        print(f"{op['id']}  {op['time']}  {op['action']:8} {op['done']:5}  {op['query']}{flag}")


def cmd_serve(a):
    from .server import run

    run()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailotter", description="A little help for a lot of mail.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="save your Google 'Desktop app' client JSON")
    s.add_argument("client_json")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("login", help="sign in a Gmail account under a nickname")
    s.add_argument("alias")
    s.add_argument("--email", help="address to pre-fill on Google's sign-in page")
    s.add_argument("--manual", action="store_true", help="paste the redirect address yourself")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("accounts", help="list signed-in accounts")
    s.set_defaults(fn=cmd_accounts)

    s = sub.add_parser("logout", help="forget an account on this computer")
    s.add_argument("alias")
    s.set_defaults(fn=cmd_logout)

    s = sub.add_parser("search", help="search with Gmail syntax")
    s.add_argument("query")
    s.add_argument("-a", "--account", default="all")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("read", help="read one email")
    s.add_argument("account")
    s.add_argument("message_id")
    s.set_defaults(fn=cmd_read)

    s = sub.add_parser("top-senders", help="who fills your inbox")
    s.add_argument("-a", "--account", default="all")
    s.add_argument("-q", "--query", default="in:inbox")
    s.add_argument("--sample", type=int, default=200)
    s.add_argument("--show", type=int, default=25)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_top)

    for name, fn, text in (("trash", cmd_trash, "move matching mail to Trash"), ("archive", cmd_archive, "remove matching mail from Inbox")):
        s = sub.add_parser(name, help=f"{text} (preview first)")
        s.add_argument("query")
        s.add_argument("-a", "--account", default="all")
        s.add_argument("-y", "--yes", action="store_true", help="skip the 'yes' question")
        s.set_defaults(fn=fn)

    s = sub.add_parser("undo", help="reverse a trash/archive")
    s.add_argument("op_id", nargs="?", default="last")
    s.set_defaults(fn=cmd_undo)

    s = sub.add_parser("history", help="recent actions")
    s.add_argument("-n", "--limit", type=int, default=10)
    s.set_defaults(fn=cmd_history)

    s = sub.add_parser("serve", help="run as an MCP server (stdio) for Claude and other assistants")
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        sys.exit(f"mailotter: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
