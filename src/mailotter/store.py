"""Where MailOtter keeps its files, and the account list.

Config (keys, tokens, account list):  $MAILOTTER_HOME or ~/.config/mailotter
State (pending previews, action log): <config>/state

Everything is written owner-only (0600 files, 0700 dirs).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def home() -> Path:
    env = os.environ.get("MAILOTTER_HOME")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "mailotter"


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def config_dir() -> Path:
    return _private_dir(home())


def tokens_dir() -> Path:
    return _private_dir(config_dir() / "tokens")


def state_dir() -> Path:
    return _private_dir(config_dir() / "state")


def client_secret_path() -> Path:
    return config_dir() / "client_secret.json"


def write_private(path: Path, text: str) -> None:
    """Write a file that only the owner can read, without a readable window."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def check_alias(alias: str) -> str:
    if not ALIAS_RE.match(alias or ""):
        raise ValueError(
            "Account nickname must be 1-32 characters: lowercase letters, digits, '-' or '_'."
        )
    return alias


def accounts() -> dict[str, str]:
    """alias -> email address."""
    return read_json(config_dir() / "accounts.json", {})


def save_account(alias: str, email: str) -> None:
    data = accounts()
    data[check_alias(alias)] = email
    write_private(config_dir() / "accounts.json", json.dumps(data, indent=2, sort_keys=True))


def remove_account(alias: str) -> bool:
    data = accounts()
    if alias not in data:
        return False
    del data[alias]
    write_private(config_dir() / "accounts.json", json.dumps(data, indent=2, sort_keys=True))
    token = tokens_dir() / f"{alias}.json"
    if token.exists():
        token.unlink()
    return True


def resolve(account: str) -> list[str]:
    """Turn 'all', an alias, or an email address into a list of aliases."""
    data = accounts()
    if not data:
        raise ValueError("No accounts yet. Run: mailotter login <nickname>")
    if account == "all":
        return sorted(data)
    if account in data:
        return [account]
    for alias, email in data.items():
        if email.lower() == (account or "").lower():
            return [alias]
    raise ValueError(f"Unknown account '{account}'. Known: {', '.join(sorted(data))}, or 'all'.")
