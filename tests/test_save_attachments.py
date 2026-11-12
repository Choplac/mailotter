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
    assert files(tmp_path / "out") == ["2026-11-08-m1.pdf"]   # one kept, so no suffix
    assert (tmp_path / "out" / "2026-11-08-m1.pdf").read_bytes() == PDF
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
    assert files(tmp_path / "out") == ["2026-11-08-m1.pdf"]
    assert (tmp_path / "out" / "2026-11-08-m1.pdf").read_bytes() == PDF2
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
