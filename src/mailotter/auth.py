"""Google sign-in, one token per account.

MailOtter asks Google only for the `gmail.modify` scope: read, label, archive
and move to Trash. That scope cannot send mail and cannot permanently delete.
"""

from __future__ import annotations

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import store

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
MANUAL_REDIRECT = "http://127.0.0.1:1/"


def _flow() -> InstalledAppFlow:
    path = store.client_secret_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Google client key not found at {path}. Download the 'Desktop app' OAuth client "
            "JSON from Google Cloud and run: mailotter setup <path-to-json>"
        )
    return InstalledAppFlow.from_client_secrets_file(str(path), scopes=SCOPES)


def login(alias: str, email_hint: str | None = None, manual: bool = False) -> str:
    """Sign in one account and save its token. Returns the signed-in address."""
    store.check_alias(alias)
    flow = _flow()
    extra = {"login_hint": email_hint} if email_hint else {}
    if manual:
        # For machines where the browser cannot reach a local port: the browser
        # lands on an error page, and the user pastes that page's address here.
        flow.redirect_uri = MANUAL_REDIRECT
        url, _ = flow.authorization_url(prompt="consent", access_type="offline", **extra)
        print("1. Open this link and sign in:\n\n" + url + "\n")
        print("2. The browser then shows a 'can't connect' page. Copy its full address and paste it here.")
        response = input("Address: ").strip()
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # loopback http only
        flow.fetch_token(authorization_response=response)
        creds = flow.credentials
    else:
        creds = flow.run_local_server(
            host="localhost",
            bind_addr="127.0.0.1",
            port=0,
            open_browser=False,
            authorization_prompt_message="Open this link in your browser and sign in:\n\n{url}\n",
            success_message="MailOtter is signed in. You can close this tab.",
            prompt="consent",
            access_type="offline",
            **extra,
        )
    if not set(SCOPES) <= set(creds.scopes or SCOPES):
        raise RuntimeError("Google did not grant the Gmail access MailOtter needs. Try again and tick the box.")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    email = service.users().getProfile(userId="me").execute()["emailAddress"]
    store.write_private(store.tokens_dir() / f"{alias}.json", creds.to_json())
    store.save_account(alias, email)
    return email


def credentials(alias: str) -> Credentials:
    path = store.tokens_dir() / f"{alias}.json"
    if not path.exists():
        raise RuntimeError(f"Account '{alias}' is not signed in. Run: mailotter login {alias}")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:  # google.auth.exceptions.RefreshError and network errors
                raise RuntimeError(
                    f"Sign-in for '{alias}' expired or was revoked. Run: mailotter login {alias}"
                ) from exc
            store.write_private(path, creds.to_json())
        else:
            raise RuntimeError(f"Sign-in for '{alias}' is not usable. Run: mailotter login {alias}")
    return creds


def service(alias: str):
    return build("gmail", "v1", credentials=credentials(alias), cache_discovery=False)
