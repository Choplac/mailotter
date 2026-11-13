"""Save the attachments of matching mail to a local folder (read-only on Gmail).

The attachment's own filename is written by a stranger, so it never shapes the
path: files are named <YYYY-MM-DD>-<message id>[-<n>].<ext>, where <n> is the
attachment's position in the email (stable across reruns, whatever failed) and
<ext> is .pdf for content that really is a PDF, otherwise a short lowercase
extension from the original name (or the MIME type when there is no name); the
same in both --pdf-only modes. Existing files are never overwritten, and a symlink
or non-file at the target name is refused. A file appears under its final name
only complete: it is written to a hidden ".mailotter-*.part" temp file, synced,
then hard-linked into place, which fails rather than replace anything there.
"""

from __future__ import annotations

import hashlib
import mimetypes
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


def _ext(filename: str, mime: str = "") -> str:
    last = re.split(r"[/\\]", filename or "")[-1]
    ext = last.rsplit(".", 1)[1].lower() if "." in last else ""
    if not _EXT.fullmatch(ext):
        ext = (mimetypes.guess_extension(mime or "") or "").lstrip(".").lower()
    return "." + ext if _EXT.fullmatch(ext) else ".bin"


def _prepare_dir(path: Path) -> None:
    if path.is_dir():
        return  # an existing folder keeps its permissions
    if path.exists() or path.is_symlink():
        raise ValueError(f"{path} exists and is not a folder.")
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)  # mkdir's mode is reduced by the umask; this folder is ours


def _existing(path: Path, data: bytes) -> str | None:
    """Outcome when something already sits at path, or None if nothing does."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return "refused"  # a symlink, folder or device: never write through it
    with open(path, "rb") as f:
        same = hashlib.sha256(f.read()).digest() == hashlib.sha256(data).digest()
    return "already saved" if same else "conflict"


def _write_new(path: Path, data: bytes) -> str:
    """Write data at path unless something is there. Returns the outcome."""
    found = _existing(path, data)
    if found:
        return found
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mailotter-", suffix=".part")  # mode 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)  # atomic, and never replaces what another process put there
        except FileExistsError:
            return _existing(path, data) or "conflict"
    finally:
        os.unlink(tmp)
    return "saved"


def save_attachments(
    account: str, query: str, to, pdf_only: bool = False, limit: int = 20, factory=None
) -> dict:
    """Returns {"results": [{"status", "name", "size"}], "more": bool, "unreadable": int}.
    status: saved / already saved / conflict / refused / skipped / too large / failed / not written."""
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
    results, unreadable = [], 0
    for msg in box.attachments(ids):  # one message at a time: saved before the next is fetched
        if msg.get("unreadable"):
            unreadable += 1
            continue
        if not _SAFE_ID.fullmatch(msg["id"] or ""):
            results.append({"status": "failed", "name": "(unexpected message id)", "size": 0})
            continue
        day = datetime.fromtimestamp(msg["internal_date"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        for n, f in enumerate(msg["files"], start=1):  # n = position in the email, not among successes
            # The name never depends on --pdf-only: a real PDF is always .pdf, so runs with
            # and without the flag find each other's files.
            is_pdf = f["data"] is not None and f["data"].startswith(b"%PDF-")
            ext = ".pdf" if is_pdf else _ext(f["filename"], f["mime"])
            name = f"{day}-{msg['id']}{'' if n == 1 else f'-{n}'}{ext}"
            if f["error"]:
                status = "too large" if f["error"] == "too large" else "failed"
            elif pdf_only and not is_pdf:
                status = "skipped"
            else:
                try:
                    status = _write_new(folder / name, f["data"])
                except OSError:
                    status = "not written"
            results.append({"status": status, "name": name, "size": f["size"]})
            f["data"] = None
    return {"results": results, "more": more, "unreadable": unreadable}
