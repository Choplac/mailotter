# MailOtter Privacy Policy

_Last updated: November 7, 2026_

MailOtter is free, open-source software that runs entirely on your own computer.
There is no MailOtter server, company or account.

## What MailOtter accesses

When you sign in a Gmail account, you give MailOtter Google's `gmail.modify`
permission. MailOtter uses it only to:

- search and read your messages when you (or the AI assistant you run it with) ask it to;
- count senders;
- move messages to Trash or out of the Inbox after you confirm, and put them back when you undo.

MailOtter has no feature that sends email or permanently deletes it.

## Where your data goes

- **Your email** goes directly between your computer and Google's Gmail API. Nobody
  else receives it, including the MailOtter authors.
- **If you connect MailOtter to an AI assistant** (such as Claude), the email content
  you ask about is passed to that assistant. That assistant's own privacy policy
  then applies.
- **Stored on your computer only:** your sign-in tokens, your account list, and a log
  of the actions you confirmed (message IDs, not content), in `~/.config/mailotter`.
  Only your user account can read these files.

MailOtter does not collect analytics, does not sell or share data, and does not
use your data for advertising or to train AI models.

## Google API Services User Data Policy

MailOtter's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Removing access

Run `mailotter logout <nickname>` to delete a sign-in from your computer, and
remove MailOtter at <https://myaccount.google.com/permissions> to revoke Google access.

## Contact

Open an issue at <https://github.com/Choplac/mailotter/issues>.
