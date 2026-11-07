<p align="center"><img src="assets/logo.png" width="180" alt="MailOtter logo"></p>

<h1 align="center">MailOtter</h1>

**A little help for a lot of mail.**

MailOtter lets your AI assistant (Claude, or any MCP client) search and clean up
**all your Gmail accounts at once**, without the scary parts:

- 🔍 **Search everywhere.** One search across every inbox you own.
- 📊 **Top senders.** Find out who's actually filling your inbox.
- 🗑️ **Bulk trash, two steps.** Preview first ("43 emails, here are 5"), act only after you say yes.
- ↩️ **Undo anything.** Every trash or archive is logged and can be reversed.
- 📥 **Archive** to clear the inbox without deleting.

And what it **can't** do, by design:

- ✉️ **It can't send email.** MailOtter has no send function at all. (Honest note: Google's `gmail.modify` permission, which MailOtter needs for Trash, would technically allow sending. The guarantee comes from MailOtter's code, which tests check.)
- 🔥 **It can't permanently delete.** Mail goes to Trash, which you can recover for 30 days.
- 🧩 **It can't do half a job.** If a search matches more than the limit (2,000 by default), it refuses and changes nothing.
- ⏱️ **It can't act on stale previews.** A preview code works once, for 15 minutes, on exactly the emails it showed. Mail that arrives later is never touched, and mail already changed since the preview is skipped.
- 🔌 **It can't half-connect.** Every account is reached before anything changes. A crash mid-way still leaves a journal, so `undo` works.

It runs on your own computer (Linux, macOS or WSL). Your mail goes only between your computer and Google.

It deliberately stays under Gmail's per-account speed limit, so big jobs take time: about 5 minutes to trash 1,000 emails, and about 1 minute to scan 250 for top senders.

## Install

```bash
pipx install git+https://github.com/Choplac/mailotter
```

## One-time Google setup (about 15 minutes)

Gmail needs a "client key" that belongs to you:

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and **create a project** (any name).
2. Under **APIs & Services → Library**, open **Gmail API** and click **Enable**.
3. Open **Google Auth Platform → Get started**. App name: `MailOtter`. Audience: **External**.
4. Under **Audience → Test users**, add every Gmail address you want to manage.
5. Under **Clients → Create client**, choose **Desktop app**, then click **Download JSON**.
6. Run `mailotter setup ~/Downloads/client_secret_XXXX.json`, then delete the download.

> While your Google app is in **Testing**, Google makes sign-ins expire after 7 days.
> To avoid re-signing weekly, open **Audience** and click **Publish app**. It stays private to you,
> and Google shows an "unverified app" warning at sign-in, which is expected for a personal tool.

## Sign in your accounts

```bash
mailotter login personal --email you@gmail.com
mailotter login old --email old.you@gmail.com
mailotter accounts
```

`login` prints a link. Open it, sign in, and allow access. If your browser can't reach the
computer (some WSL, SSH or container setups), use `mailotter login personal --manual`.

## Use it from the command line

```bash
mailotter top-senders                          # who fills your inboxes
mailotter search "from:newsletter@shop.com"    # across all accounts
mailotter trash "from:newsletter@shop.com older_than:3m"   # preview, then type yes
mailotter archive "category:promotions" -a personal
mailotter history
mailotter undo                                 # reverse the last action
```

The search words are [Gmail's own](https://support.google.com/mail/answer/7190).

## Use it from Claude Code

```bash
claude mcp add mailotter -- mailotter serve
```

Then just ask: *"Who sends me the most mail across all my accounts? Preview trashing the
newsletters older than 3 months."*

**Keep yourself in the loop:** when Claude asks permission to run `confirm_action`, read the
preview first. Don't add `confirm_action` to your always-allow list.

### Tools

| Tool | What it does | Changes mail? |
|---|---|---|
| `list_accounts` | Your accounts and nicknames | No |
| `search_mail` | Gmail search in one account or `all` | No |
| `read_mail` | Read one email (plain text) | No |
| `top_senders` | Biggest senders over recent mail | No |
| `preview_trash` / `preview_archive` | Count and examples, plus a one-time code | No |
| `confirm_action` | Carry out a preview | Yes (reversible) |
| `undo_action` | Reverse a confirmed action | Yes (restores) |
| `action_history` | Recent actions and their ids | No |

## Safety notes

- **Emails are written by strangers.** MailOtter tells the assistant never to follow instructions
  found inside an email, and the two-step confirm means a sneaky email can't trash your mail on its own.
- **Files** live in `~/.config/mailotter` (or `$MAILOTTER_HOME`) and only your user can read them:
  `client_secret.json`, `tokens/<nickname>.json`, `state/history.jsonl`.
- **To revoke access completely,** run `mailotter logout <nickname>` and remove MailOtter at
  <https://myaccount.google.com/permissions>.
- **Undo works for as long as Gmail keeps Trash** (30 days).

## Develop

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest        # uses an in-memory fake Gmail, never a real inbox
```

## License

MIT. See also the [privacy policy](PRIVACY.md).
