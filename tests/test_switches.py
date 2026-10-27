"""The auto-approve switch file (backend/auto_approve.txt)."""
from backend import switches


def test_on_and_off(tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("# comment\non\n", encoding="utf-8")
    assert switches.auto_approve_from_file(f)
    f.write_text("# comment\noff\n", encoding="utf-8")
    assert not switches.auto_approve_from_file(f)


def test_missing_or_unclear_file_is_off(tmp_path):
    assert not switches.auto_approve_from_file(tmp_path / "missing.txt")
    (tmp_path / "s.txt").write_text("yes\n", encoding="utf-8")
    assert not switches.auto_approve_from_file(tmp_path / "s.txt")


def test_environment_wins(monkeypatch):
    monkeypatch.setenv("LABX_AUTO_APPROVE", "0")
    switches.load_switches()
    import os
    assert os.environ["LABX_AUTO_APPROVE"] == "0"


def test_the_committed_switch_is_readable():
    assert switches.AUTO_APPROVE_FILE.is_file()
