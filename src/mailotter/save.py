"""Save the attachments of matching mail to a local folder (read-only on Gmail).

The attachment's own filename is written by a stranger, so it never shapes the
path: files are named <YYYY-MM-DD>-<message id>[-<n>].<ext>, where only a short
lowercase extension is taken from the original name. Existing files are never
overwritten, and a symlink or non-file at the target name is refused.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import actions, store
from .mailbox import ATTACHMENT_LIMIT

_EXT = re.compile(r"[a-z0-9]{1,8}")
_SAFE_ID = re.compile(r"[A-Za-z0-9]{1,64}")


def _ext(filename: str) -> str:
    last = re.split(r"[/\\]", filename or "")[-1]
    ext = last.rsplit(".", 1)[1].lower() if "." in last else ""
    return "." + ext if _EXT.fullmatch(ext) else ".bin"


def _prepare_dir(path: Path) -> None:
    if path.is_dir():
        return  # an existing folder keeps its permissions
    if path.exists() or path.is_symlink():
        raise ValueError(f"{path} exists and is not a folder.")
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)  # mkdir's mode is reduced by the umask; this folder is ours


def _write_new(path: Path, data: bytes) -> str:
    """Write data at path unless something is there. Returns the outcome."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISREG(st.st_mode):
            return "refused"  # a symlink, folder or device: never write through it
        with open(path, "rb") as f:
            same = hashlib.sha256(f.read()).digest() == hashlib.sha256(data).digest()
        return "already saved" if same else "conflict"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mailotter-", suffix=".part")  # mode 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return "saved"


def save_attachments(
    account: str, query: str, to, pdf_only: bool = False, limit: int = 20, factory=None
) -> dict:
    """Returns {"results": [{"status", "name", "size"}], "more": bool, "unreadable": int}.
    status: saved / already saved / conflict / refused / skipped / too large / failed."""
    query = (query or "").strip()
    if not query:
        raise ValueError("A search query is required (an empty query would match everything).")
    aliases = store.resolve(account)
    if len(aliases) != 1:
        raise ValueError("Name one account to save attachments from.")
    folder = Path(to).expanduser()
    _prepare_dir(folder)
    box = (factory or actions.default_factory)(aliases[0])
    ids, more = box.ids(query, limit)
    messages, unreadable = box.attachments(ids)
    results = []
    for msg in messages:
        if not _SAFE_ID.fullmatch(msg["id"] or ""):
            results.append({"status": "failed", "name": "(unexpected message id)", "size": 0})
            continue
        day = datetime.fromtimestamp(msg["internal_date"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        stem = f"{day}-{msg['id']}"
        keep = []
        for f in msg["files"]:
            if f["error"]:
                status = "too large" if f["error"] == "too large" else "failed"
                results.append({"status": status, "name": f"{stem} attachment", "size": f["size"]})
            elif pdf_only and not f["data"].startswith(b"%PDF-"):
                results.append({"status": "skipped", "name": f"{stem} attachment", "size": f["size"]})
            else:
                keep.append(f)
        for n, f in enumerate(keep, start=1):
            ext = ".pdf" if pdf_only else _ext(f["filename"])
            name = f"{stem}{'' if n == 1 else f'-{n}'}{ext}"
            results.append({"status": _write_new(folder / name, f["data"]), "name": name, "size": f["size"]})
    return {"results": results, "more": more, "unreadable": unreadable}
