"""Repository behaviour, run against BOTH adapters: local files and Supabase (in-memory fake, no network).
Synthetic data only."""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from backend.storage import LocalRepository, Session, StorageError, VersionConflict, get_repository
from backend.supabase_repo import SupabaseRepository
from tests.fake_supabase import FakeSupabase

WS, OTHER = "ws-a", "ws-b"
FID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PDF = b"%PDF-1.4 synthetic original"


def _meta(file_id=FID, uploaded_at="2026-10-20T08:00:00+00:00"):
    return {"file_id": file_id, "filename": "synthetic.pdf", "page_count": 1, "processing_status": "QUEUED",
            "split_status": "UNCERTAIN", "uploaded_at": uploaded_at}


@pytest.fixture(params=["local", "supabase"])
def make_repo(request, tmp_path):
    """Returns a factory: each call is a NEW repository instance over the SAME stored data (a restart)."""
    if request.param == "local":
        return lambda: LocalRepository(tmp_path)
    fake = FakeSupabase()
    for workspace in (WS, OTHER):
        fake.tables["lx_workspace_members"].append({"user_id": f"user-{workspace}", "workspace_id": workspace})
    factory = lambda: SupabaseRepository(fake.base_url, fake.key, "labextract-originals", http=fake)
    factory.fake = fake
    return factory


@pytest.fixture
def repo(make_repo):
    return make_repo()


def _with_document(repo, file_id=FID, workspace=WS):
    repo.create_document(workspace, _meta(file_id), PDF, extension=".pdf", sha256="0" * 64, content_type="application/pdf")


# ---- persistence ----------------------------------------------------------------------------
def test_document_and_original_survive_a_restart(make_repo):
    _with_document(make_repo())
    fresh = make_repo()
    assert fresh.get_document(WS, FID) == _meta()
    assert fresh.read_original(WS, FID) == (PDF, ".pdf")
    assert fresh.list_documents(WS) == [_meta()]


def test_drafts_survive_a_restart_with_their_version(make_repo):
    repo = make_repo()
    _with_document(repo)
    repo.init_drafts(WS, FID, [{"form_id": "f1"}])
    assert repo.save_drafts(WS, FID, [{"form_id": "f1", "edited": True}], expected_version=1) == 2
    assert make_repo().get_drafts(WS, FID) == ([{"form_id": "f1", "edited": True}], 2)


def test_init_drafts_keeps_existing_drafts(repo):
    _with_document(repo)
    repo.init_drafts(WS, FID, [{"form_id": "first"}])
    repo.save_drafts(WS, FID, [{"form_id": "edited"}], expected_version=1)
    assert repo.init_drafts(WS, FID, [{"form_id": "second"}]) == ([{"form_id": "edited"}], 2)


def test_list_is_newest_first(repo):
    _with_document(repo)
    second = "b" * 32
    repo.create_document(WS, _meta(second, "2026-10-20T09:00:00+00:00"), PDF, extension=".pdf", sha256="1" * 64, content_type="application/pdf")
    assert [m["file_id"] for m in repo.list_documents(WS)] == [second, FID]


# ---- no silent overwrite --------------------------------------------------------------------
def test_stale_version_is_rejected_and_nothing_is_written(repo):
    _with_document(repo)
    repo.init_drafts(WS, FID, [{"v": 1}])
    repo.save_drafts(WS, FID, [{"v": 2}], expected_version=1)
    with pytest.raises(VersionConflict) as conflict:
        repo.save_drafts(WS, FID, [{"v": "stale"}], expected_version=1)
    assert conflict.value.server_version == 2
    assert repo.get_drafts(WS, FID) == ([{"v": 2}], 2)


def test_concurrent_saves_one_wins(tmp_path):
    repo = LocalRepository(tmp_path)
    _with_document(repo)
    repo.init_drafts(WS, FID, [{"v": 0}])
    results, start = [], threading.Barrier(8)

    def save(n):
        start.wait()
        try:
            repo.save_drafts(WS, FID, [{"v": n}], expected_version=1)
            results.append("ok")
        except VersionConflict:
            results.append("conflict")

    threads = [threading.Thread(target=save, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("conflict") == 7
    assert repo.get_drafts(WS, FID)[1] == 2
    assert not list(tmp_path.glob("*.tmp"))


# ---- workspace isolation --------------------------------------------------------------------
def test_other_workspace_sees_nothing(repo):
    _with_document(repo)
    repo.init_drafts(WS, FID, [{"form_id": "f1"}])
    assert repo.get_document(OTHER, FID) is None
    assert repo.read_original(OTHER, FID) is None
    assert repo.list_documents(OTHER) == []
    assert repo.get_drafts(OTHER, FID) is None
    with pytest.raises(KeyError):
        repo.save_drafts(OTHER, FID, [{"hijack": True}], expected_version=1)
    with pytest.raises(KeyError):
        repo.init_drafts(OTHER, FID, [{"hijack": True}])
    assert repo.get_drafts(WS, FID) == ([{"form_id": "f1"}], 1)


def test_invalid_file_ids_never_reach_storage(repo):
    from fastapi import HTTPException
    for bad in ("../x", "..\\x", "A" * 32):
        with pytest.raises(HTTPException):
            repo.get_document(WS, bad)


# ---- sessions --------------------------------------------------------------------------------
def test_sessions_persist_and_revoke(make_repo):
    repo = make_repo()
    expires = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)
    repo.create_session("a" * 64, Session("user-ws-a", WS, expires))
    repo.create_session("b" * 64, Session("user-ws-a", WS, expires))
    repo.create_session("c" * 64, Session("user-ws-b", OTHER, expires))
    fresh = make_repo()
    assert fresh.get_session("a" * 64) == Session("user-ws-a", WS, expires, False)
    fresh.revoke_session("a" * 64)
    assert make_repo().get_session("a" * 64).revoked is True
    fresh.revoke_user_sessions("user-ws-a")
    assert fresh.get_session("b" * 64).revoked is True
    assert fresh.get_session("c" * 64).revoked is False
    assert fresh.get_session("d" * 64) is None


# ---- adapter specifics -----------------------------------------------------------------------
def test_local_reads_files_written_before_the_repository(tmp_path):
    (tmp_path / f"{FID}.json").write_text('{"file_id": "%s", "filename": "old.pdf"}' % FID, encoding="utf-8")
    (tmp_path / f"{FID}.pdf").write_bytes(PDF)
    (tmp_path / f"{FID}.drafts.json").write_text('[{"form_id": "f1"}]', encoding="utf-8")
    repo = LocalRepository(tmp_path)
    assert repo.read_original("default", FID) == (PDF, ".pdf")
    assert repo.get_drafts("default", FID) == ([{"form_id": "f1"}], 1)
    assert repo.get_document(WS, FID) is None                       # legacy files belong to "default" only


def test_supabase_failed_metadata_insert_removes_the_orphan_original():
    fake = FakeSupabase()
    repo = SupabaseRepository(fake.base_url, fake.key, "labextract-originals", http=fake)
    fake.fail_next["POST lx_documents"] = 500
    with pytest.raises(StorageError):
        _with_document(repo)
    assert fake.objects == {} and fake.tables["lx_documents"] == []


def test_supabase_membership_requires_exactly_one_workspace():
    fake = FakeSupabase()
    repo = SupabaseRepository(fake.base_url, fake.key, "labextract-originals", http=fake)
    fake.tables["lx_workspace_members"] += [{"user_id": "u1", "workspace_id": WS},
                                         {"user_id": "u2", "workspace_id": WS},
                                         {"user_id": "u2", "workspace_id": OTHER}]
    assert repo.get_membership("u1") == WS
    assert repo.get_membership("u2") is None                        # ambiguous: refused, not guessed
    assert repo.get_membership("nobody") is None


def test_supabase_original_is_stored_under_its_workspace():
    fake = FakeSupabase()
    repo = SupabaseRepository(fake.base_url, fake.key, "labextract-originals", http=fake)
    _with_document(repo)
    assert list(fake.objects) == [f"labextract-originals/{WS}/{FID}.pdf"]


def test_local_storage_is_refused_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("LABEXTRACT_STORAGE", raising=False)
    with pytest.raises(RuntimeError, match="not persistent"):
        get_repository.__wrapped__()


def test_supabase_storage_needs_its_environment(monkeypatch):
    monkeypatch.setenv("LABEXTRACT_STORAGE", "supabase")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        get_repository.__wrapped__()
