import os
import stat

import pytest

from mailotter import actions, cli, save, store
from mailotter.mailbox import Mailbox

from fake_gmail import FakeGmail

PDF = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"
PDF2 = b"%PDF-1.4\nanother report\n%%EOF\n"
WHEN = "1794103200000"  # 2026-11-08 02:00 UTC (still Nov 7 in America)
READ_ONLY_CALLS = {"list", "get", "attachments.get"}


def mail(mid, attachments, sender="reports@example.com", subject="report"):
    return {"id": mid, "from": sender, "subject": subject, "labels": ["INBOX"],
            "internalDate": WHEN, "attachments": attachments}


@pytest.fixture
def box(tmp_path, monkeypatch):
    """One account 'work' with a fake mailbox; returns (fake, run) where run calls the CLI."""
    monkeypatch.setenv("MAILOTTER_HOME", str(tmp_path / "home"))
    store.save_account("work", "work@example.com")
    fake = FakeGmail([])
    monkeypatch.setattr(actions, "default_factory", lambda alias: Mailbox(alias, fake, throttle=False))

    def run(*extra, to=None, query="from:reports@example.com"):
        dest = to or tmp_path / "out"
        try:
            cli.main(["save-attachments", query, "-a", "work", "--to", str(dest), *extra])
            return 0
        except SystemExit as exc:
            return exc.code

    return fake, run


def files(path):
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


def test_saves_pdf_by_attachment_id_and_inline(box, tmp_path, capsys):
    fake, run = box
    fake.store.update({m["id"]: {**m, "labels": {"INBOX"}} for m in (
        mail("a1b2", [{"filename": "Report.PDF", "data": PDF}]),
        mail("c3d4", [{"filename": "invoice.pdf", "data": PDF2, "inline": True}]),
    )})
    assert run() == 0
    out = tmp_path / "out"
    assert files(out) == ["2026-11-08-a1b2.pdf", "2026-11-08-c3d4.pdf"]
    assert (out / "2026-11-08-a1b2.pdf").read_bytes() == PDF
    assert (out / "2026-11-08-c3d4.pdf").read_bytes() == PDF2
    assert fake.api.count("attachments.get") == 1   # the inline one needs no fetch
    text = capsys.readouterr().out
    assert "saved 2026-11-08-a1b2.pdf" in text and "Report.PDF" not in text


def test_second_attachment_gets_number_suffix(box, tmp_path):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF}, {"filename": "b.CSV", "data": b"x,y\n"},
                                      {"filename": "c.pdf", "data": PDF2}]), "labels": {"INBOX"}}
    assert run() == 0
    assert files(tmp_path / "out") == ["2026-11-08-m1-2.csv", "2026-11-08-m1-3.pdf", "2026-11-08-m1.pdf"]


def test_pdf_only_checks_the_bytes_not_the_name(box, tmp_path, capsys):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "x.pdf", "data": b"<html>not a pdf</html>", "mime": "application/pdf"},
                                      {"filename": "scan.bin", "data": PDF}]), "labels": {"INBOX"}}
    assert run("--pdf-only") == 0
    assert files(tmp_path / "out") == ["2026-11-08-m1-2.pdf"]   # numbered by its place in the email
    assert (tmp_path / "out" / "2026-11-08-m1-2.pdf").read_bytes() == PDF
    assert "skipped, not a PDF" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["../../etc/passwd", "/etc/passwd", "..\\..\\evil.exe/", "a.verylongext1",
                                  "x.p/df", "x.pd f", "x.", ".bashrc/..", "x.PDF/../../../y"])
def test_untrusted_filename_cannot_shape_the_path(box, tmp_path, name):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": name, "data": b"data"}]), "labels": {"INBOX"}}
    assert run() == 0
    saved = files(tmp_path / "out")
    assert saved == ["2026-11-08-m1.bin"]
    assert {p.name for p in tmp_path.iterdir()} == {"home", "out"}
    assert not os.path.exists("/etc/passwd.bin") and not os.path.exists("/y")


def test_extension_is_lowercased_and_kept_when_clean(box, tmp_path):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "Data Export.XLSX", "data": b"PK"}]), "labels": {"INBOX"}}
    assert run() == 0
    assert files(tmp_path / "out") == ["2026-11-08-m1.xlsx"]


def test_rerun_with_same_content_is_a_no_op(box, tmp_path, capsys):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF}]), "labels": {"INBOX"}}
    assert run() == 0
    target = tmp_path / "out" / "2026-11-08-m1.pdf"
    before = os.stat(target)
    capsys.readouterr()
    assert run() == 0
    assert "already saved 2026-11-08-m1.pdf" in capsys.readouterr().out
    after = os.stat(target)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_different_existing_file_is_not_overwritten(box, tmp_path, capsys):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF}]), "labels": {"INBOX"}}
    out = tmp_path / "out"
    out.mkdir()
    (out / "2026-11-08-m1.pdf").write_bytes(b"mine, keep me")
    assert run() != 0
    assert (out / "2026-11-08-m1.pdf").read_bytes() == b"mine, keep me"
    assert "conflict 2026-11-08-m1.pdf" in capsys.readouterr().out
    assert files(out) == ["2026-11-08-m1.pdf"]   # no temp file left behind


def test_symlink_at_target_is_refused(box, tmp_path):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF}]), "labels": {"INBOX"}}
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    (out / "2026-11-08-m1.pdf").symlink_to(victim)
    assert run() != 0
    assert victim.read_bytes() == b"precious"
    assert (out / "2026-11-08-m1.pdf").is_symlink()


def test_directory_at_target_is_refused(box, tmp_path):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF}]), "labels": {"INBOX"}}
    (tmp_path / "out" / "2026-11-08-m1.pdf").mkdir(parents=True)
    assert run() != 0


def test_new_dir_is_private_and_existing_dir_mode_is_kept(box, tmp_path):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF}]), "labels": {"INBOX"}}
    new = tmp_path / "deep" / "new"
    assert run(to=new) == 0
    assert stat.S_IMODE(os.stat(new).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(new / "2026-11-08-m1.pdf").st_mode) == 0o600
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    assert run(to=shared) == 0
    assert stat.S_IMODE(os.stat(shared).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(shared / "2026-11-08-m1.pdf").st_mode) == 0o600


def test_oversized_attachment_is_refused_without_download(box, tmp_path, capsys):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "big.pdf", "data": PDF, "size": 25 * 1024 * 1024 + 1},
                                      {"filename": "small.pdf", "data": PDF2}]), "labels": {"INBOX"}}
    assert run() != 0
    assert files(tmp_path / "out") == ["2026-11-08-m1-2.pdf"]
    assert (tmp_path / "out" / "2026-11-08-m1-2.pdf").read_bytes() == PDF2
    assert fake.api.count("attachments.get") == 1
    assert "over 25 MB" in capsys.readouterr().out


def test_only_read_calls_are_made(box, tmp_path, capsys):
    fake, run = box
    fake.store.update({m["id"]: {**m, "labels": {"INBOX"}} for m in (
        mail("m1", [{"filename": "a.pdf", "data": PDF}, {"filename": "b.txt", "data": b"hi", "inline": True}]),
        mail("m2", [{"filename": "c.pdf", "data": PDF2}]),
    )})
    assert run() == 0
    assert "Saved 3," in capsys.readouterr().out
    assert run("--pdf-only") == 0
    assert set(fake.api) <= READ_ONLY_CALLS and "attachments.get" in fake.api
    assert fake.calls == []                       # no trash/untrash/label change recorded
    assert all(m["labels"] == {"INBOX"} for m in fake.store.values())


def test_limit_caps_messages(box, tmp_path, capsys):
    fake, run = box
    for i in range(3):
        fake.store[f"m{i}"] = {**mail(f"m{i}", [{"filename": "a.pdf", "data": PDF}]), "labels": {"INBOX"}}
    assert run("--limit", "2") == 0
    assert len(files(tmp_path / "out")) == 2
    assert "Only the newest 2" in capsys.readouterr().out


def test_names_one_account(box, tmp_path):
    store.save_account("home", "home@example.com")
    with pytest.raises(ValueError, match="one account"):
        save.save_attachments("all", "has:attachment", tmp_path / "out")


# ---- review findings ---------------------------------------------------------

def _race(monkeypatch, path, content):
    """Make another process create `path` while our temp file is being written."""
    real = os.fsync

    def fsync(fd):
        if not path.exists():
            path.write_bytes(content)
        return real(fd)

    monkeypatch.setattr(save.os, "fsync", fsync)


def test_file_created_meanwhile_by_another_process_is_not_overwritten(box, tmp_path, monkeypatch, capsys):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF}]), "labels": {"INBOX"}}
    target = tmp_path / "out" / "2026-11-08-m1.pdf"
    _race(monkeypatch, target, b"theirs, keep me")
    assert run() != 0
    assert target.read_bytes() == b"theirs, keep me"
    assert "conflict 2026-11-08-m1.pdf" in capsys.readouterr().out
    assert files(tmp_path / "out") == ["2026-11-08-m1.pdf"]   # temp file cleaned up


def test_same_file_created_meanwhile_counts_as_already_saved(box, tmp_path, monkeypatch, capsys):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF}]), "labels": {"INBOX"}}
    _race(monkeypatch, tmp_path / "out" / "2026-11-08-m1.pdf", PDF)
    assert run() == 0
    assert "already saved 2026-11-08-m1.pdf" in capsys.readouterr().out
    assert files(tmp_path / "out") == ["2026-11-08-m1.pdf"]


def test_write_failure_leaves_no_file_and_is_reported(box, tmp_path, monkeypatch, capsys):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF}, {"filename": "b.pdf", "data": PDF2}]),
                        "labels": {"INBOX"}}

    def fsync(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(save.os, "fsync", fsync)
    assert run() != 0
    assert files(tmp_path / "out") == []
    assert capsys.readouterr().out.count("could not write:") == 2


def test_leftover_temp_file_is_never_taken_as_saved(box, tmp_path):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF}]), "labels": {"INBOX"}}
    out = tmp_path / "out"
    out.mkdir()
    (out / ".mailotter-abc123.part").write_bytes(PDF[:5])   # an interrupted earlier run
    assert run() == 0
    assert (out / "2026-11-08-m1.pdf").read_bytes() == PDF


def test_attachment_id_is_fetched_even_when_data_is_empty(box, tmp_path):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF, "empty_data": True}]), "labels": {"INBOX"}}
    assert run("--pdf-only") == 0
    assert (tmp_path / "out" / "2026-11-08-m1.pdf").read_bytes() == PDF
    assert fake.api.count("attachments.get") == 1


def test_failed_first_attachment_does_not_shift_names(box, tmp_path, capsys):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "a.pdf", "data": PDF, "fail": True},
                                      {"filename": "b.pdf", "data": PDF2}]), "labels": {"INBOX"}}
    out = tmp_path / "out"
    assert run() != 0
    assert files(out) == ["2026-11-08-m1-2.pdf"]
    assert "failed to load: 2026-11-08-m1.pdf" in capsys.readouterr().out
    fake.store["m1"]["attachments"][0]["fail"] = False      # Gmail works again
    assert run() == 0
    assert files(out) == ["2026-11-08-m1-2.pdf", "2026-11-08-m1.pdf"]
    assert (out / "2026-11-08-m1.pdf").read_bytes() == PDF
    assert (out / "2026-11-08-m1-2.pdf").read_bytes() == PDF2


def test_attachments_without_a_name_are_saved(box, tmp_path):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [
        {"filename": "", "data": b"<p>big html body</p>", "mime": "text/html"},       # a body, not an attachment
        {"filename": "", "data": PDF, "mime": "application/pdf", "disposition": 'attachment; size=40'},
        {"filename": "", "data": PDF2, "mime": "application/pdf"},                      # only an attachmentId
        {"filename": "", "data": b"x,y\n", "mime": "text/csv", "inline": True, "disposition": "attachment"},
    ]), "labels": {"INBOX"}}
    assert run() == 0
    out = tmp_path / "out"
    assert files(out) == ["2026-11-08-m1-2.pdf", "2026-11-08-m1-3.csv", "2026-11-08-m1.pdf"]
    assert (out / "2026-11-08-m1.pdf").read_bytes() == PDF
    assert (out / "2026-11-08-m1-2.pdf").read_bytes() == PDF2
    only = tmp_path / "pdfs"
    assert run("--pdf-only", to=only) == 0
    assert files(only) == ["2026-11-08-m1-2.pdf", "2026-11-08-m1.pdf"]


def test_saves_message_by_message_and_reports_a_failed_one(box, tmp_path, monkeypatch, capsys):
    fake, run = box
    fake.store.update({m["id"]: {**m, "labels": {"INBOX"}} for m in (
        mail("m1", [{"filename": "a.pdf", "data": PDF}]),
        mail("m2", [{"filename": "b.pdf", "data": PDF2, "fail": True}]),
        mail("m3", [{"filename": "c.pdf", "data": PDF2}]),
    )})
    out = tmp_path / "out"
    seen = {}
    real = fake._attachment

    def spy(mid, aid):
        seen.setdefault(mid, files(out))
        return real(mid, aid)

    monkeypatch.setattr(fake, "_attachment", spy)
    assert run() != 0
    assert seen["m3"] == ["2026-11-08-m1.pdf"]       # m1 was on disk before m3 was fetched
    assert files(out) == ["2026-11-08-m1.pdf", "2026-11-08-m3.pdf"]
    assert "failed to load: 2026-11-08-m2.pdf" in capsys.readouterr().out


# ---- review round 2 ------------------------------------------------------------

def test_nameless_multipart_attachment_is_reported_not_skipped(box, tmp_path, capsys):
    fake, run = box
    text = {"mimeType": "text/plain", "filename": "", "body": {"size": 2, "data": "aGk"}}
    container = {"mimeType": "multipart/mixed", "filename": "", "body": {"size": 0},
                 "headers": [{"name": "Content-Disposition", "value": "attachment"}], "parts": [text, dict(text)]}
    fake.store["m1"] = {**mail("m1", [{"part": container}, {"filename": "a.pdf", "data": PDF}]), "labels": {"INBOX"}}
    assert run() != 0
    assert files(tmp_path / "out") == ["2026-11-08-m1-2.pdf"]     # the body texts are not saved as files
    assert "failed to load: 2026-11-08-m1" in capsys.readouterr().out


@pytest.mark.parametrize("first", [(), ("--pdf-only",)])
def test_pdf_only_flag_does_not_change_names(box, tmp_path, capsys, first):
    fake, run = box
    fake.store["m1"] = {**mail("m1", [{"filename": "x.pdf", "data": b"<html>not a pdf</html>"},
                                      {"filename": "scan.bin", "data": PDF}]), "labels": {"INBOX"}}
    second = () if first else ("--pdf-only",)
    assert run(*first) == 0
    capsys.readouterr()
    assert run(*second) == 0
    assert "already saved 2026-11-08-m1-2.pdf" in capsys.readouterr().out
    assert (tmp_path / "out" / "2026-11-08-m1-2.pdf").read_bytes() == PDF
    assert [n for n in files(tmp_path / "out") if n.startswith("2026-11-08-m1-2")] == ["2026-11-08-m1-2.pdf"]
