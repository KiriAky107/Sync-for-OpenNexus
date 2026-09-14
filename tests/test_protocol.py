"""两个受控设备的黑盒协议向量；只操作 pytest 临时目录。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import uuid

from fastapi.testclient import TestClient
import pytest

from sync_server.app import create_app
from sync_server.database import Database, run
from sync_server.storage import DiskObjects


@pytest.fixture
def env(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "sync.db"))
    now = [1000000]
    store = DiskObjects(tmp_path / "objects")
    app = create_app(db, store, tmp_path / "staging", clock=lambda: now[0])
    db.add_user("alice", "controlled-fixture-password")
    db.add_user("bob", "controlled-fixture-password")
    with TestClient(app) as client:
        yield client, db, store, now
    db.engine.dispose()


def session(client, user="alice"):
    response = client.post("/sync/v1/auth/sessions", json={"username": user, "password": "controlled-fixture-password", "device_name": "测试设备"})
    assert response.status_code == 200, response.text
    data = response.json()
    return {"Authorization": "Bearer " + data["access_token"]}, data


def setup(client):
    auth, token = session(client)
    vault = client.post("/sync/v1/vaults", json={"name": "隔离笔记"}, headers=auth).json()["vault_id"]
    return auth, token, "/sync/v1/vaults/" + vault


def upload(client, base, auth, data=b"controlled note"):
    sha = hashlib.sha256(data).hexdigest()
    info = client.post(base + "/uploads", headers=auth, json={"content_hash": sha, "size": len(data)}).json()
    if not info["complete"]:
        path = base + "/uploads/" + info["upload_id"]
        assert client.put(path + "?offset=0", headers=auth, content=data).status_code == 200
        assert client.post(path + "/complete", headers=auth).status_code == 200
    return sha


def change(sha, **overrides):
    return {"operation_id": uuid.uuid4().hex, "file_id": uuid.uuid4().hex,
            "base_revision": 0, "path": "中文/笔记.md", "operation": "put", "content_hash": sha,
            "size": len(b"controlled note"), **overrides}


def test_two_devices_conflict_retry_move_delete_history(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    second, _ = session(client)
    sha = upload(client, base, auth)
    body = change(sha)
    first = client.post(base + "/revisions", headers=auth, json=body)
    assert first.status_code == 200
    assert client.post(base + "/revisions", headers=auth, json=body).json() == first.json()
    other = {**body, "operation_id": uuid.uuid4().hex}
    assert client.post(base + "/revisions", headers=second, json=other).json()["error"]["code"] == "REVISION_CONFLICT"
    move = {**other, "base_revision": 1, "path": "中文/移动.md"}
    assert client.post(base + "/revisions", headers=second, json=move).json()["sequence"] == 2
    deletion = {**move, "operation_id": uuid.uuid4().hex, "base_revision": 2, "operation": "delete", "content_hash": None, "size": 0}
    assert client.post(base + "/revisions", headers=second, json=deletion).json()["sequence"] == 3
    assert client.post(base + "/revisions", headers=auth, json={**body, "operation_id": uuid.uuid4().hex}).status_code == 409
    history = client.get(base + "/history/" + body["file_id"], headers=auth).json()["items"]
    assert [x["sequence"] for x in history] == [3, 2, 1]
    # 恢复生成新 Revision，旧历史和对象保持不变。
    restore = {**body, "operation_id": uuid.uuid4().hex, "base_revision": 3}
    assert client.post(base + "/revisions", headers=auth, json=restore).json()["sequence"] == 4
    assert client.get(base + "/objects/" + sha, headers=auth).content == b"controlled note"


def test_object_isolation_revocation_refresh_and_expiry(env):
    client, _, _, now = env
    auth, token, base = setup(client)
    second, _ = session(client)
    outsider, _ = session(client, "bob")
    sha = upload(client, base, auth)
    assert client.get(base + "/objects/" + sha, headers=outsider).status_code == 404
    refreshed = client.post("/sync/v1/auth/refresh", json={"refresh_token": token["refresh_token"]}).json()
    assert client.get(base + "/objects/" + sha, headers=auth).status_code == 401
    assert client.post("/sync/v1/auth/refresh", json={"refresh_token": token["refresh_token"]}).status_code == 401
    auth = {"Authorization": "Bearer " + refreshed["access_token"]}
    assert client.delete("/sync/v1/devices/" + token["device_id"], headers=second).status_code == 204
    assert client.get(base + "/objects/" + sha, headers=auth).status_code == 401
    assert client.post("/sync/v1/auth/refresh", json={"refresh_token": refreshed["refresh_token"]}).status_code == 401
    now[0] += 901
    assert client.get(base + "/changes", headers=second).status_code == 401


@pytest.mark.parametrize("path", ["../a", "/a", "a\\b", "CON.md", "a/aux", "a.", "a ", "a//b", ".ainote/db", "e\u0301.md", "x:y", "a\x00b"])
def test_unsafe_paths(env, path):
    client, _, _, _ = env
    auth, _, base = setup(client)
    assert client.post(base + "/revisions", headers=auth, json=change("0" * 64, path=path)).status_code == 422


def test_resume_integrity_quota_and_missing_object(env):
    client, db, _, _ = env
    auth, _, base = setup(client)
    sha = hashlib.sha256(b"abc").hexdigest()
    info = client.post(base + "/uploads", headers=auth, json={"content_hash": sha, "size": 3}).json()
    path = base + "/uploads/" + info["upload_id"]
    assert client.put(path + "?offset=0", headers=auth, content=b"a").json()["offset"] == 1
    assert client.put(path + "?offset=0", headers=auth, content=b"a").status_code == 409
    assert client.get(path, headers=auth).json()["offset"] == 1
    assert client.post(path + "/complete", headers=auth).status_code == 422
    assert client.put(path + "?offset=1", headers=auth, content=b"bc").status_code == 200
    assert client.post(path + "/complete", headers=auth).status_code == 200
    assert client.post(base + "/revisions", headers=auth, json=change("0" * 64)).status_code == 409
    with db.transaction() as conn:
        run(conn, "UPDATE vaults SET quota=3")
    assert client.post(base + "/uploads", headers=auth, json={"content_hash": "0" * 64, "size": 1}).status_code == 413


def test_concurrent_cas_and_fixed_cursor_boundary(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    sha = upload(client, base, auth)
    body = change(sha)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(base + "/revisions", headers=auth, json={**body, "operation_id": uuid.uuid4().hex}).status_code, range(2)))
    assert sorted(results) == [200, 409]
    snapshot = client.get(base + "/changes?limit=1", headers=auth).json()
    assert snapshot["boundary"] == 1
    assert client.post(base + "/revisions", headers=auth, json=change(sha, path="第二篇.md")).status_code == 200
    assert client.get(base + "/changes?cursor=1&boundary=1", headers=auth).json()["items"] == []
    assert len(client.get(base + "/changes?cursor=1", headers=auth).json()["items"]) == 1


def test_casefold_parent_path_collision_and_idempotency(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    sha = upload(client, base, auth)
    body = change(sha, path="A.md")
    assert client.post(base + "/revisions", headers=auth, json=body).status_code == 200
    for path in ["a.MD", "a.md/child"]:
        assert client.post(base + "/revisions", headers=auth, json=change(sha, path=path)).json()["error"]["code"] == "PATH_CONFLICT"
    assert client.post(base + "/revisions", headers=auth, json={**body, "path": "other"}).json()["error"]["code"] == "IDEMPOTENCY_REUSED"


def test_login_limits_and_protocol(env):
    client, _, _, _ = env
    assert client.get("/sync/v1/handshake?protocol=2").status_code == 426
    for _ in range(10):
        assert client.post("/sync/v1/auth/sessions", json={"username": "unknown", "password": "controlled-fixture-password", "device_name": "fixture"}).status_code == 401
    assert client.post("/sync/v1/auth/sessions", json={"username": "unknown", "password": "controlled-fixture-password", "device_name": "fixture"}).status_code == 429


def test_bootstrap_password_rotates_until_account_is_fixed(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "bootstrap.db"))
    db.migrate()
    first = db.prepare_bootstrap_user()
    second = db.prepare_bootstrap_user()
    assert first["username"] == second["username"] == "admin"
    assert first["password"] != second["password"]

    app = create_app(db, DiskObjects(tmp_path / "objects"), tmp_path / "staging")
    with TestClient(app) as client:
        old = client.post("/sync/v1/auth/sessions", json={
            "username": "admin", "password": first["password"], "device_name": "旧启动",
        })
        assert old.status_code == 401
        login = client.post("/sync/v1/auth/sessions", json={
            "username": "admin", "password": second["password"], "device_name": "首次登录",
        })
        assert login.status_code == 200
        assert login.json()["must_change_credentials"] is True
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        blocked = client.get("/sync/v1/vaults", headers=headers)
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "CREDENTIAL_CHANGE_REQUIRED"

        changed = client.put("/sync/v1/account/credentials", headers=headers, json={
            "current_password": second["password"],
            "username": "owner",
            "password": "fixed-production-password",
        })
        assert changed.json() == {"username": "owner", "credentials_fixed": True}
        assert client.post("/sync/v1/vaults", headers=headers, json={"name": "固定账户"}).status_code == 200

    assert db.prepare_bootstrap_user() is None
    with TestClient(app) as client:
        login = client.post("/sync/v1/auth/sessions", json={
            "username": "owner", "password": "fixed-production-password", "device_name": "重启后",
        })
        assert login.status_code == 200
        assert login.json()["must_change_credentials"] is False
    db.engine.dispose()
