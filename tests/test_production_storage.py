"""Fault vectors for the production IO paths; database still uses an isolated fixture."""
import hashlib
import pytest
from fastapi.testclient import TestClient
from sync_server.app import create_app
from sync_server.database import Database, row
from sync_server.maintenance import cleanup_expired_uploads
from sync_server.storage import DiskObjects
from test_protocol import setup, upload


@pytest.fixture
def env(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "sync.db"))
    store = DiskObjects(tmp_path / "objects")
    staging = tmp_path / "staging"
    now = [1000]
    app = create_app(db, store, staging, clock=lambda: now[0])
    db.add_user("alice", "controlled-fixture-password")
    with TestClient(app) as client:
        yield client, db, store, staging, now
    db.engine.dispose()


def test_offset_reconciliation_never_acknowledges_missing_disk_bytes(env):
    client, db, _, staging, _ = env
    auth, _, base = setup(client)
    info = client.post(base + "/uploads", headers=auth,
                       json={"content_hash": hashlib.sha256(b"abc").hexdigest(), "size": 3}).json()
    path = base + "/uploads/" + info["upload_id"]
    assert client.put(path + "?offset=0", headers=auth, content=b"a").status_code == 200
    local = staging / info["upload_id"]
    local.write_bytes(b"abc")
    assert client.get(path, headers=auth).json()["offset"] == 1
    assert local.read_bytes() == b"a"
    local.write_bytes(b"")
    assert client.get(path, headers=auth).json()["error"]["code"] == "UPLOAD_DAMAGED"
    assert not local.exists()
    assert client.put(path + "?offset=1", headers=auth, content=b"bc").status_code == 404
    with db.transaction() as conn:
        assert row(conn, "SELECT COUNT(*) AS n FROM uploads")["n"] == 0
    replacement = client.post(
        base + "/uploads",
        headers=auth,
        json={"content_hash": hashlib.sha256(b"abc").hexdigest(), "size": 3},
    ).json()
    assert replacement["complete"] is False
    assert replacement["upload_id"] != info["upload_id"]


def test_complete_retry_has_durable_receipt_and_charges_once(env):
    client, db, _, staging, now = env
    auth, _, base = setup(client)
    info = client.post(base + "/uploads", headers=auth,
                       json={"content_hash": hashlib.sha256(b"abc").hexdigest(), "size": 3}).json()
    path = base + "/uploads/" + info["upload_id"]
    assert client.put(path + "?offset=0", headers=auth, content=b"abc").status_code == 200
    first = client.post(path + "/complete", headers=auth)
    assert first.status_code == 200
    assert not (staging / info["upload_id"]).exists()
    for _ in range(100):
        retry = client.post(path + "/complete", headers=auth)
        assert retry.status_code == 200
        assert retry.json() == first.json()
    assert client.post(path + "/complete").status_code == 401
    with db.transaction() as conn:
        assert row(conn, "SELECT used FROM vaults WHERE id=:id", id=base.rsplit("/", 1)[1])["used"] == 3
        assert row(conn, "SELECT COUNT(*) AS n FROM upload_receipts")["n"] == 1


def test_download_is_verified_before_response_and_temp_files_are_removed(env):
    client, _, store, staging, _ = env
    auth, _, base = setup(client)
    sha = upload(client, base, auth)
    # This path must use streaming open(), never the full-object get().
    store.get = lambda key: pytest.fail("full-object read")
    response = client.get(base + "/objects/" + sha, headers=auth)
    assert response.content == b"controlled note"
    assert list(staging.glob("download-*")) == []
    (store.root / base.rsplit("/", 1)[1] / sha).write_bytes(b"corruption")
    response = client.get(base + "/objects/" + sha, headers=auth)
    assert response.status_code == 503
    assert b"corruption" not in response.content
    assert list(staging.glob("download-*")) == []


def test_cleanup_only_expired_staging_preserves_committed_objects(env):
    client, db, store, staging, now = env
    auth, _, base = setup(client)
    sha = upload(client, base, auth)
    info = client.post(base + "/uploads", headers=auth,
                       json={"content_hash": hashlib.sha256(b"pending").hexdigest(), "size": 7}).json()
    assert cleanup_expired_uploads(db, staging, now=1001)["expired_uploads_removed"] == 0
    now[0] += 3601
    assert cleanup_expired_uploads(db, staging, now=now[0])["expired_uploads_removed"] == 1
    assert not (staging / info["upload_id"]).exists()
    assert cleanup_expired_uploads(db, staging, now=now[0])["expired_uploads_removed"] == 0
    assert store.get(base.rsplit("/",1)[1] + "/" + sha) == b"controlled note"
    with db.transaction() as conn:
        assert row(conn,"SELECT used FROM vaults")["used"] == len(b"controlled note")


def test_readiness_fails_closed_when_object_storage_unavailable(env):
    client, _, store, _, _ = env
    def unavailable(*args):
        raise OSError("simulated outage")
    store.put = unavailable
    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 200


def test_slow_upload_fsync_does_not_block_worker_health(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import os

    client, _, _, _, _ = env
    auth, _, base = setup(client)
    info = client.post(base + "/uploads", headers=auth,
                       json={"content_hash": hashlib.sha256(b"abc").hexdigest(), "size": 3}).json()
    path = base + "/uploads/" + info["upload_id"]
    entered, release = Event(), Event()
    original = os.fsync

    def delayed(fd):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release fsync")
        original(fd)

    monkeypatch.setattr(os, "fsync", delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(client.put, path + "?offset=0", headers=auth, content=b"abc")
        try:
            assert entered.wait(5)
            # Both requests use this TestClient's single ASGI event loop.
            health = pool.submit(client.get, "/health").result(timeout=2)
            assert health.status_code == 200
            assert not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=5).json() == {"offset": 3}
    assert client.get(path, headers=auth).json()["offset"] == 3
    assert client.post(path + "/complete", headers=auth).status_code == 200


def test_upload_limits_and_failed_fsync_preserve_durable_offset(env, monkeypatch):
    import os

    client, _, _, staging, _ = env
    auth, _, base = setup(client)
    data = b"a" * 1048576
    info = client.post(base + "/uploads", headers=auth,
                       json={"content_hash": hashlib.sha256(data).hexdigest(), "size": len(data)}).json()
    path = base + "/uploads/" + info["upload_id"]
    rejected = client.put(path + "?offset=0", headers=auth, content=data + b"x")
    assert rejected.status_code == 413
    assert rejected.json()["error"]["code"] == "CHUNK_TOO_LARGE"
    assert (staging / info["upload_id"]).stat().st_size == 0

    def failed(_fd):
        raise OSError("controlled fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", failed)
        with pytest.raises(OSError, match="controlled fsync failure"):
            client.put(path + "?offset=0", headers=auth, content=data)
    # Failure propagated from the thread; the SQL transaction did not advance.
    assert client.get(path, headers=auth).json()["offset"] == 0
    assert (staging / info["upload_id"]).stat().st_size == 0
    assert client.put(path + "?offset=0", headers=auth, content=data).json() == {"offset": len(data)}
    assert client.post(path + "/complete", headers=auth).status_code == 200
