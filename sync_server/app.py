"""Sync v1 HTTP 边界；每次访问重新检查设备撤销，内容不进入日志。"""

import hashlib
import json
import secrets
import time
import os
import tempfile
import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from starlette.background import BackgroundTask

from .database import Database, password_hash, row, rows, run
from .models import Commit, Login, Refresh, Upload, VaultCreate


class SyncError(Exception):
    def __init__(self, status, code, details=None):
        self.status, self.code, self.details = status, code, details or {}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_app(db: Database, objects, staging: Path, *, quota=1024**3, clock=time.time):
    db.migrate()
    staging.mkdir(parents=True, exist_ok=True)
    @asynccontextmanager
    async def lifespan(_app):
        from .maintenance import cleanup_expired_uploads
        stopping = asyncio.Event()

        async def maintain():
            while not stopping.is_set():
                try:
                    await asyncio.to_thread(cleanup_expired_uploads, db, staging, now=int(clock()))
                except Exception:
                    logging.getLogger(__name__).error("UPLOAD_MAINTENANCE_FAILED")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=60)
                except TimeoutError:
                    pass

        worker = asyncio.create_task(maintain())
        try:
            yield
        finally:
            stopping.set()
            await worker

    app = FastAPI(title="OpenNexus Sync", version="1.0.0", lifespan=lifespan)
    app.state.database = db

    @app.exception_handler(SyncError)
    async def error(_request, exc):
        headers = {"Retry-After": "60"} if exc.status == 429 else {}
        return JSONResponse({"error": {"code": exc.code, "details": exc.details}}, status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _exc):
        # Pydantic 的原始错误可能带请求正文，禁止回显密码或笔记。
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "details": {}}}, status_code=422)

    def identity(conn, authorization):
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        session = row(conn, "SELECT s.*, d.user_id, d.revoked FROM sessions s JOIN devices d ON d.id=s.device_id WHERE s.token=:token", token=digest(token))
        if not session or session["revoked"] or session["expires"] <= clock():
            raise SyncError(401, "SESSION_EXPIRED")
        return session

    def vault(conn, vault_id, authorization, *, lock=False):
        session = identity(conn, authorization)
        # Vault 行锁覆盖配额、CAS、路径冲突和序列分配，跨 Worker 也保持一致。
        suffix = " FOR UPDATE" if lock and not db.sqlite else ""
        item = row(conn, "SELECT * FROM vaults WHERE id=:id AND user_id=:owner" + suffix,
                   id=vault_id, owner=session["user_id"])
        if not item:
            raise SyncError(404, "VAULT_NOT_FOUND")
        return session, item

    def issue(conn, device_id):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        run(conn, "INSERT INTO sessions VALUES (:token,:refresh,:device,:expires,:refresh_expires)",
            token=digest(access), refresh=digest(refresh), device=device_id,
            expires=int(clock()) + 900, refresh_expires=int(clock()) + 30 * 86400)
        return {"access_token": access, "refresh_token": refresh, "expires_in": 900, "device_id": device_id}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    def readiness_probe():
        with db.transaction() as conn:
            if row(conn, "SELECT version FROM schema_version")["version"] != 1:
                raise SyncError(503, "SCHEMA_INCOMPATIBLE")
        key = "health-probe/" + secrets.token_hex(16)
        try:
            with tempfile.TemporaryFile(dir=staging) as local:
                local.write(b"opennexus-ready")
                local.flush()
                os.fsync(local.fileno())
                local.seek(0)
                if local.read() != b"opennexus-ready":
                    raise OSError("STAGING_INTEGRITY")
            objects.put(key, b"opennexus-ready")
            if objects.get(key) != b"opennexus-ready":
                raise OSError("STORAGE_INTEGRITY")
        finally:
            objects.delete(key)

    ready_lock = asyncio.Lock()
    ready_cache = {"until": 0.0, "ok": False}

    @app.get("/ready")
    async def ready():
        async with ready_lock:
            if time.monotonic() >= ready_cache["until"]:
                try:
                    await asyncio.wait_for(asyncio.to_thread(readiness_probe), timeout=3)
                    ready_cache["ok"] = True
                except Exception:
                    ready_cache["ok"] = False
                ready_cache["until"] = time.monotonic() + 5
            if not ready_cache["ok"]:
                raise SyncError(503, "DEPENDENCY_UNAVAILABLE")
            return {"status": "ready", "schema": 1}

    @app.get("/sync/v1/handshake")
    def handshake(protocol: int = 1):
        if protocol != 1:
            raise SyncError(426, "PROTOCOL_INCOMPATIBLE")
        return {"protocol": 1, "max_object_size": 104857600, "chunk_size": 1048576,
                "encryption": "transport-only", "history_retention": "indefinite",
                "cursor_retention": "indefinite", "sharing": False}

    @app.post("/sync/v1/auth/sessions")
    def login(body: Login, request: Request):
        key = digest((request.client.host if request.client else "unknown") + ":" + body.username.casefold())
        # 失败计数先独立提交，抛出认证异常也不会回滚限流状态。
        with db.transaction() as conn:
            limit = row(conn, "SELECT * FROM login_limits WHERE key=:key", key=key)
            if limit and clock() - limit["started"] < 60:
                if limit["attempts"] >= 10:
                    raise SyncError(429, "RATE_LIMITED")
                run(conn, "UPDATE login_limits SET attempts=attempts+1 WHERE key=:key", key=key)
            else:
                run(conn, "DELETE FROM login_limits WHERE key=:key", key=key)
                run(conn, "INSERT INTO login_limits VALUES (:key,:now,1)", key=key, now=int(clock()))
        with db.transaction() as conn:
            user = row(conn, "SELECT * FROM users WHERE username=:name", name=body.username)
            expected = user["password"] if user else password_hash("unavailable-user-password", "0" * 32)
            if not secrets.compare_digest(password_hash(body.password, expected.split(":")[0]), expected) or not user:
                raise SyncError(401, "LOGIN_FAILED")
            device_id = secrets.token_hex(16)
            run(conn, "INSERT INTO devices VALUES (:id,:user,:name,0)", id=device_id, user=user["id"], name=body.device_name)
            return issue(conn, device_id)

    @app.post("/sync/v1/auth/refresh")
    def refresh(body: Refresh):
        with db.transaction() as conn:
            suffix = " FOR UPDATE OF s" if not db.sqlite else ""
            session = row(conn, "SELECT s.*,d.revoked FROM sessions s JOIN devices d ON d.id=s.device_id WHERE refresh=:refresh" + suffix,
                          refresh=digest(body.refresh_token))
            if not session or session["revoked"] or session["refresh_expires"] <= clock():
                raise SyncError(401, "SESSION_EXPIRED")
            run(conn, "DELETE FROM sessions WHERE token=:token", token=session["token"])
            return issue(conn, session["device_id"])

    @app.delete("/sync/v1/auth/sessions", status_code=204)
    def logout(authorization: str = Header(default="")):
        with db.transaction() as conn:
            session = identity(conn, authorization)
            run(conn, "DELETE FROM sessions WHERE token=:token", token=session["token"])

    @app.get("/sync/v1/devices")
    def devices(authorization: str = Header(default="")):
        with db.transaction() as conn:
            session = identity(conn, authorization)
            return {"items": rows(conn, "SELECT id,name,revoked FROM devices WHERE user_id=:user", user=session["user_id"])}

    @app.delete("/sync/v1/devices/{device_id}", status_code=204)
    def revoke(device_id: str, authorization: str = Header(default="")):
        with db.transaction() as conn:
            session = identity(conn, authorization)
            run(conn, "UPDATE devices SET revoked=1 WHERE id=:id AND user_id=:user", id=device_id, user=session["user_id"])

    @app.post("/sync/v1/vaults")
    def create_vault(body: VaultCreate, authorization: str = Header(default="")):
        with db.transaction() as conn:
            session = identity(conn, authorization)
            vault_id = secrets.token_hex(16)
            run(conn, "INSERT INTO vaults VALUES (:id,:user,:name,0,:quota,0)", id=vault_id, user=session["user_id"], name=body.name, quota=quota)
            return {"vault_id": vault_id, "name": body.name}

    @app.get("/sync/v1/vaults")
    def list_vaults(authorization: str = Header(default="")):
        with db.transaction() as conn:
            session = identity(conn, authorization)
            return {"items": rows(conn, "SELECT id,name,sequence,used,quota FROM vaults WHERE user_id=:user", user=session["user_id"])}

    @app.post("/sync/v1/vaults/{vault_id}/uploads")
    def begin_upload(vault_id: str, body: Upload, authorization: str = Header(default="")):
        with db.transaction() as conn:
            session, item = vault(conn, vault_id, authorization, lock=True)
            found = row(conn, "SELECT * FROM objects WHERE vault_id=:v AND hash=:h", v=vault_id, h=body.content_hash)
            if found:
                if found["size"] != body.size:
                    raise SyncError(409, "OBJECT_SIZE_MISMATCH")
                return {"complete": True, "upload_id": None, "offset": body.size}
            reserved = row(conn, "SELECT COALESCE(SUM(size),0) AS size FROM uploads WHERE vault_id=:v AND expires>:now", v=vault_id, now=int(clock()))["size"]
            if item["used"] + reserved + body.size > item["quota"]:
                raise SyncError(413, "QUOTA_EXCEEDED")
            upload_id = secrets.token_hex(16)
            run(conn, "INSERT INTO uploads VALUES (:id,:v,:device,:h,:size,0,:expires)", id=upload_id,
                v=vault_id, device=session["device_id"], h=body.content_hash, size=body.size, expires=int(clock()) + 3600)
            (staging / upload_id).write_bytes(b"")
            return {"complete": False, "upload_id": upload_id, "offset": 0}

    def authorized_upload(conn, vault_id, upload_id, authorization):
        session, _ = vault(conn, vault_id, authorization, lock=True)
        upload = row(conn, "SELECT * FROM uploads WHERE id=:id AND vault_id=:v AND device_id=:device", id=upload_id, v=vault_id, device=session["device_id"])
        if not upload or upload["expires"] <= clock():
            raise SyncError(404, "UPLOAD_EXPIRED")
        return upload

    def reconcile_staging(upload):
        path = staging / upload["id"]
        try:
            length = path.stat().st_size
            if length < upload["offset_bytes"]:
                raise SyncError(409, "UPLOAD_DAMAGED", {"restart_required": True})
            if length > upload["offset_bytes"]:
                with path.open("r+b") as stream:
                    stream.truncate(upload["offset_bytes"])
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError:
            raise SyncError(409, "UPLOAD_DAMAGED", {"restart_required": True}) from None
        return path

    @app.get("/sync/v1/vaults/{vault_id}/uploads/{upload_id}")
    def upload_status(vault_id: str, upload_id: str, authorization: str = Header(default="")):
        with db.transaction() as conn:
            upload = authorized_upload(conn, vault_id, upload_id, authorization)
            reconcile_staging(upload)
            return {"offset": upload["offset_bytes"], "size": upload["size"], "expires": upload["expires"]}

    @app.put("/sync/v1/vaults/{vault_id}/uploads/{upload_id}")
    async def upload_chunk(vault_id: str, upload_id: str, request: Request,
                           offset: int = Query(ge=0), authorization: str = Header(default="")):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 1048576:
                raise SyncError(413, "CHUNK_TOO_LARGE")
        with db.transaction() as conn:
            upload = authorized_upload(conn, vault_id, upload_id, authorization)
            reconcile_staging(upload)
            if offset != upload["offset_bytes"]:
                raise SyncError(409, "UPLOAD_OFFSET", {"offset": upload["offset_bytes"]})
            if offset + len(data) > upload["size"]:
                raise SyncError(413, "OBJECT_TOO_LARGE")
            # 先刷盘后提交 offset；崩溃重试覆盖未确认尾部，不能重复追加。
            import os
            with (staging / upload_id).open("r+b") as stream:
                stream.seek(offset)
                stream.write(data)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
            run(conn, "UPDATE uploads SET offset_bytes=:offset WHERE id=:id", id=upload_id, offset=offset + len(data))
            return {"offset": offset + len(data)}

    @app.delete("/sync/v1/vaults/{vault_id}/uploads/{upload_id}", status_code=204)
    def cancel_upload(vault_id: str, upload_id: str, authorization: str = Header(default="")):
        with db.transaction() as conn:
            authorized_upload(conn, vault_id, upload_id, authorization)
            run(conn, "DELETE FROM uploads WHERE id=:id", id=upload_id)
        (staging / upload_id).unlink(missing_ok=True)

    @app.post("/sync/v1/vaults/{vault_id}/uploads/{upload_id}/complete")
    def complete_upload(vault_id: str, upload_id: str, authorization: str = Header(default="")):
        with db.transaction() as conn:
            session, _ = vault(conn, vault_id, authorization, lock=True)
            receipt = row(conn, "SELECT hash FROM upload_receipts WHERE id=:id AND vault_id=:v AND device_id=:d",
                          id=upload_id, v=vault_id, d=session["device_id"])
            if receipt:
                return {"complete": True, "content_hash": receipt["hash"]}
            upload = authorized_upload(conn, vault_id, upload_id, authorization)
            path = reconcile_staging(upload)
            with path.open("rb") as stream:
                content_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            if upload["size"] != upload["offset_bytes"] or content_hash != upload["hash"]:
                raise SyncError(422, "OBJECT_INTEGRITY")
            exists = row(conn, "SELECT hash FROM objects WHERE vault_id=:v AND hash=:h", v=vault_id, h=upload["hash"])
            if not exists:
                objects.put_file(vault_id + "/" + upload["hash"], path, upload["hash"])
                run(conn, "INSERT INTO objects VALUES (:v,:h,:size,:now)", v=vault_id, h=upload["hash"], size=upload["size"], now=int(clock()))
                run(conn, "UPDATE vaults SET used=used+:size WHERE id=:v", size=upload["size"], v=vault_id)
            run(conn, "INSERT INTO upload_receipts VALUES (:id,:v,:d,:h,:now)", id=upload_id,
                v=vault_id, d=session["device_id"], h=upload["hash"], now=int(clock()))
            run(conn, "DELETE FROM uploads WHERE id=:id", id=upload_id)
        (staging / upload_id).unlink(missing_ok=True)
        return {"complete": True, "content_hash": upload["hash"]}

    def perform_commit(conn, vault_id, body, session, item):
        fingerprint = digest(body.model_dump_json())
        previous = row(conn, "SELECT * FROM revisions WHERE vault_id=:v AND operation_id=:op", v=vault_id, op=body.operation_id)
        if previous:
            if previous["fingerprint"] != fingerprint or previous["device_id"] != session["device_id"]:
                raise SyncError(409, "IDEMPOTENCY_REUSED")
            return dict(previous)
        current = row(conn, "SELECT * FROM files WHERE vault_id=:v AND file_id=:f", v=vault_id, f=body.file_id)
        if (current["sequence"] if current else 0) != body.base_revision:
            actual = row(conn, "SELECT * FROM revisions WHERE vault_id=:v AND sequence=:s", v=vault_id, s=current["sequence"]) if current else None
            raise SyncError(409, "REVISION_CONFLICT", {"current": dict(actual) if actual else None})
        if body.operation == "put":
            obj = row(conn, "SELECT * FROM objects WHERE vault_id=:v AND hash=:h", v=vault_id, h=body.content_hash)
            if not obj or obj["size"] != body.size:
                raise SyncError(409, "OBJECT_NOT_READY")
            paths = rows(conn, "SELECT path_key FROM files WHERE vault_id=:v AND deleted=0 AND file_id<>:f", v=vault_id, f=body.file_id)
            key = body.path.casefold()
            if any(p["path_key"] == key or p["path_key"].startswith(key + "/") or key.startswith(p["path_key"] + "/") for p in paths):
                raise SyncError(409, "PATH_CONFLICT")
        elif not current or body.content_hash is not None or body.size != 0:
            raise SyncError(422, "INVALID_DELETE")
        sequence = item["sequence"] + 1
        run(conn, "UPDATE vaults SET sequence=:s WHERE id=:v", s=sequence, v=vault_id)
        run(conn, "INSERT INTO revisions VALUES (:v,:s,:f,:base,:path,:key,:operation,:hash,:size,:device,:op,:fingerprint)",
            v=vault_id, s=sequence, f=body.file_id, base=body.base_revision, path=body.path, key=body.path.casefold(),
            operation=body.operation, hash=body.content_hash, size=body.size, device=session["device_id"], op=body.operation_id, fingerprint=fingerprint)
        run(conn, "DELETE FROM files WHERE vault_id=:v AND file_id=:f", v=vault_id, f=body.file_id)
        run(conn, "INSERT INTO files VALUES (:v,:f,:s,:key,:deleted)", v=vault_id, f=body.file_id, s=sequence,
            key=body.path.casefold(), deleted=int(body.operation == "delete"))
        return dict(row(conn, "SELECT * FROM revisions WHERE vault_id=:v AND sequence=:s", v=vault_id, s=sequence))

    @app.post("/sync/v1/vaults/{vault_id}/revisions")
    def commit(vault_id: str, body: Commit, authorization: str = Header(default="")):
        with db.transaction() as conn:
            session, item = vault(conn, vault_id, authorization, lock=True)
            return perform_commit(conn, vault_id, body, session, item)

    @app.get("/sync/v1/vaults/{vault_id}/changes")
    def changes(vault_id: str, cursor: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500),
                boundary: int | None = Query(default=None, ge=0), authorization: str = Header(default="")):
        with db.transaction() as conn:
            _, item = vault(conn, vault_id, authorization)
            end = item["sequence"] if boundary is None else boundary
            if cursor > end or end > item["sequence"]:
                raise SyncError(409, "CURSOR_INVALID")
            items = rows(conn, "SELECT * FROM revisions WHERE vault_id=:v AND sequence>:cursor AND sequence<=:end ORDER BY sequence LIMIT :limit", v=vault_id, cursor=cursor, end=end, limit=limit)
            next_cursor = items[-1]["sequence"] if items else cursor
            return {"items": items, "cursor": next_cursor, "boundary": end, "has_more": next_cursor < end}

    @app.get("/sync/v1/vaults/{vault_id}/history/{file_id}")
    def history(vault_id: str, file_id: str, before: int = Query(default=9223372036854775807, ge=1),
                limit: int = Query(default=100, ge=1, le=500), authorization: str = Header(default="")):
        with db.transaction() as conn:
            vault(conn, vault_id, authorization)
            return {"items": rows(conn, "SELECT * FROM revisions WHERE vault_id=:v AND file_id=:f AND sequence<:before ORDER BY sequence DESC LIMIT :limit", v=vault_id, f=file_id, before=before, limit=limit)}

    @app.get("/sync/v1/vaults/{vault_id}/objects/{content_hash}")
    def get_object(vault_id: str, content_hash: str, authorization: str = Header(default="")):
        with db.transaction() as conn:
            vault(conn, vault_id, authorization)
            obj = row(conn, "SELECT * FROM objects WHERE vault_id=:v AND hash=:h", v=vault_id, h=content_hash)
            if not obj:
                raise SyncError(404, "OBJECT_NOT_FOUND")
            expected_size = obj["size"]
        # Verify before returning any bytes, without holding a database transaction
        # or buffering an entire attachment in RAM.
        temporary = tempfile.NamedTemporaryFile(prefix="download-", dir=staging, delete=False)
        path = Path(temporary.name)
        try:
            size, checksum = 0, hashlib.sha256()
            with temporary, objects.open(vault_id + "/" + content_hash) as source:
                while chunk := source.read(1048576):
                    size += len(chunk)
                    if size > expected_size:
                        raise SyncError(503, "STORAGE_INTEGRITY")
                    checksum.update(chunk)
                    temporary.write(chunk)
            if size != expected_size or checksum.hexdigest() != content_hash:
                raise SyncError(503, "STORAGE_INTEGRITY")
            return FileResponse(path, media_type="application/octet-stream",
                headers={"ETag": '"' + content_hash + '"', "Cache-Control": "private, no-store"},
                background=BackgroundTask(path.unlink, missing_ok=True))
        except BaseException:
            temporary.close()
            path.unlink(missing_ok=True)
            raise

    return app
