"""一致的 PostgreSQL/S3 备份和空实例恢复操作。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from sqlalchemy import text

from .database import SCHEMA


MANIFEST_SCHEMA = 1
VAULT_ID = re.compile(r"[0-9a-f]{32}")
CONTENT_HASH = re.compile(r"[0-9a-f]{64}")
TABLES: dict[str, tuple[str, ...]] = {
    "schema_version": ("version",),
    "users": ("id", "username", "password"),
    "devices": ("id", "user_id", "name", "revoked"),
    "sessions": ("token", "refresh", "device_id", "expires", "refresh_expires"),
    "vaults": ("id", "user_id", "name", "sequence", "quota", "used"),
    "uploads": ("id", "vault_id", "device_id", "hash", "size", "offset_bytes", "expires"),
    "objects": ("vault_id", "hash", "size", "created"),
    "revisions": (
        "vault_id",
        "sequence",
        "file_id",
        "base_revision",
        "path",
        "path_key",
        "operation",
        "hash",
        "size",
        "device_id",
        "operation_id",
        "fingerprint",
    ),
    "files": ("vault_id", "file_id", "sequence", "path_key", "deleted"),
    "login_limits": ("key", "started", "attempts"),
    "upload_receipts": ("id", "vault_id", "device_id", "hash", "completed"),
    "bootstrap_state": ("user_id", "created"),
}


class OperationsError(RuntimeError):
    """稳定的面向操作员的故障，无需凭证或响应主体。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object_path(root: Path, vault_id: str, content_hash: str) -> Path:
    if not VAULT_ID.fullmatch(vault_id) or not CONTENT_HASH.fullmatch(content_hash):
        raise OperationsError("OBJECT_ID_INVALID")
    return root / "objects" / vault_id / content_hash


def _manifest_objects(conn) -> list[dict[str, Any]]:
    pending = conn.execute(text("SELECT COUNT(*) FROM uploads")).scalar_one()
    if pending:
        raise OperationsError("BACKUP_PENDING_UPLOADS")
    missing = conn.execute(
        text(
            "SELECT COUNT(*) FROM revisions r LEFT JOIN objects o "
            "ON o.vault_id=r.vault_id AND o.hash=r.hash "
            "WHERE r.hash IS NOT NULL AND o.hash IS NULL"
        )
    ).scalar_one()
    if missing:
        raise OperationsError("HISTORICAL_OBJECT_MISSING")
    return [
        {"vault_id": row.vault_id, "hash": row.hash, "size": int(row.size)}
        for row in conn.execute(
            text("SELECT vault_id,hash,size FROM objects ORDER BY vault_id,hash")
        )
    ]


def _write_database_snapshot(conn, destination: Path) -> dict[str, int]:
    counts = {}
    with destination.open("x", encoding="utf-8", newline="\n") as output:
        for table, columns in TABLES.items():
            projection = ",".join(f'"{column}"' for column in columns)
            ordering = ",".join(f'"{column}"' for column in columns)
            rows = conn.execute(
                text(f'SELECT {projection} FROM "{table}" ORDER BY {ordering}')
            )
            count = 0
            for row in rows:
                output.write(
                    json.dumps(
                        {"table": table, "values": list(row)},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                count += 1
            counts[table] = count
        output.flush()
        os.fsync(output.fileno())
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return counts


def _download(objects, root: Path, item: dict[str, Any]) -> None:
    target = _object_path(root, item["vault_id"], item["hash"])
    target.parent.mkdir(parents=True, exist_ok=True)
    response = objects.client.get_object(
        Bucket=objects.bucket,
        Key=f"{item['vault_id']}/{item['hash']}",
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with response["Body"] as body, target.open("xb") as destination:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                destination.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if size != item["size"] or digest.hexdigest() != item["hash"]:
        target.unlink(missing_ok=True)
        raise OperationsError("OBJECT_INTEGRITY_FAILED")


def create_backup(db, objects, destination: Path, *, workers: int = 8) -> dict[str, Any]:
    destination = destination.resolve(strict=False)
    if destination.exists():
        raise OperationsError("BACKUP_DESTINATION_EXISTS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=destination.name + ".incomplete-", dir=destination.parent)
    )
    database = temporary / "database.jsonl"
    try:
        with db.engine.connect() as conn:
            transaction = conn.begin()
            try:
                conn.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                version = conn.execute(text("SELECT version FROM schema_version")).scalar_one()
                if version != 1:
                    raise OperationsError("SCHEMA_INCOMPATIBLE")
                catalog = _manifest_objects(conn)
                table_rows = _write_database_snapshot(conn, database)
                transaction.commit()
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda item: _download(objects, temporary, item), catalog))
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "database_sha256": sha256_file(database),
            "database_rows": table_rows,
            "object_count": len(catalog),
            "object_bytes": sum(item["size"] for item in catalog),
            "objects": catalog,
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, destination)
        return {
            "status": "BACKUP_COMPLETE",
            "created_utc": manifest["created_utc"],
            "object_count": manifest["object_count"],
            "object_bytes": manifest["object_bytes"],
        }
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _load_manifest(source: Path, max_age_hours: float) -> dict[str, Any]:
    try:
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        created = datetime.fromisoformat(manifest["created_utc"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise OperationsError("BACKUP_MANIFEST_INVALID") from error
    if manifest.get("schema") != MANIFEST_SCHEMA or created.tzinfo is None:
        raise OperationsError("BACKUP_MANIFEST_INVALID")
    age = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
    if age < -300 or age > max_age_hours * 3600:
        raise OperationsError("BACKUP_AGE_INVALID")
    catalog = manifest.get("objects")
    if not isinstance(catalog, list) or manifest.get("database_rows") is None:
        raise OperationsError("BACKUP_MANIFEST_INVALID")
    normalized = []
    for item in catalog:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("vault_id"), str)
            or not isinstance(item.get("hash"), str)
            or not isinstance(item.get("size"), int)
            or isinstance(item.get("size"), bool)
            or item["size"] < 0
        ):
            raise OperationsError("BACKUP_MANIFEST_INVALID")
        _object_path(source, item["vault_id"], item["hash"])
        normalized.append(
            {"vault_id": item["vault_id"], "hash": item["hash"], "size": item["size"]}
        )
    rows = manifest["database_rows"]
    if (
        normalized != sorted(normalized, key=lambda item: (item["vault_id"], item["hash"]))
        or len({(item["vault_id"], item["hash"]) for item in normalized}) != len(normalized)
        or manifest.get("object_count") != len(normalized)
        or manifest.get("object_bytes") != sum(item["size"] for item in normalized)
        or not CONTENT_HASH.fullmatch(str(manifest.get("database_sha256", "")))
        or not isinstance(rows, dict)
        or set(rows) != set(TABLES)
        or any(not isinstance(rows[name], int) or rows[name] < 0 for name in TABLES)
    ):
        raise OperationsError("BACKUP_MANIFEST_INVALID")
    manifest["objects"] = normalized
    manifest["age_seconds"] = max(0, int(age))
    return manifest


def _load_database_snapshot(source: Path, manifest: dict[str, Any]) -> dict[str, list[list]]:
    database = source / "database.jsonl"
    if not database.is_file() or sha256_file(database) != manifest["database_sha256"]:
        raise OperationsError("BACKUP_DATABASE_INTEGRITY_FAILED")
    restored = {table: [] for table in TABLES}
    try:
        with database.open("r", encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                table = item.get("table")
                values = item.get("values")
                if table not in TABLES or not isinstance(values, list):
                    raise OperationsError("BACKUP_DATABASE_INVALID")
                if len(values) != len(TABLES[table]):
                    raise OperationsError("BACKUP_DATABASE_INVALID")
                restored[table].append(values)
    except (OSError, json.JSONDecodeError) as error:
        raise OperationsError("BACKUP_DATABASE_INVALID") from error
    if any(len(restored[table]) != manifest["database_rows"][table] for table in TABLES):
        raise OperationsError("BACKUP_DATABASE_INVALID")
    if restored["schema_version"] != [[1]]:
        raise OperationsError("BACKUP_DATABASE_INVALID")
    object_columns = TABLES["objects"]
    indexes = {name: object_columns.index(name) for name in ("vault_id", "hash", "size")}
    catalog = sorted(
        (
            {
                "vault_id": row[indexes["vault_id"]],
                "hash": row[indexes["hash"]],
                "size": row[indexes["size"]],
            }
            for row in restored["objects"]
        ),
        key=lambda item: (item["vault_id"], item["hash"]),
    )
    if catalog != manifest["objects"]:
        raise OperationsError("BACKUP_DATABASE_INVALID")
    return restored


def _verify_backup_file(source: Path, item: dict[str, Any]) -> None:
    path = _object_path(source, item["vault_id"], item["hash"])
    try:
        size = path.stat().st_size
    except OSError as error:
        raise OperationsError("BACKUP_OBJECT_MISSING") from error
    if size != item["size"] or sha256_file(path) != item["hash"]:
        raise OperationsError("BACKUP_OBJECT_INTEGRITY_FAILED")


def _upload_and_verify(objects, source: Path, item: dict[str, Any]) -> str:
    key = f"{item['vault_id']}/{item['hash']}"
    path = _object_path(source, item["vault_id"], item["hash"])
    objects.put_file(key, path, item["hash"])
    try:
        response = objects.client.get_object(Bucket=objects.bucket, Key=key)
        digest = hashlib.sha256()
        size = 0
        with response["Body"] as body:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        if size != item["size"] or digest.hexdigest() != item["hash"]:
            raise OperationsError("RESTORED_OBJECT_INTEGRITY_FAILED")
    except BaseException:
        try:
            objects.delete(key)
        except BaseException:
            pass
        raise
    return key


def _database_is_empty(conn) -> bool:
    return (
        conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema='public'"
            )
        ).scalar_one()
        == 0
    )


def _restore_database(
    db, snapshot: dict[str, list[list]], expected_objects: list[dict[str, Any]]
) -> None:
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(1330534488)"))
        if not _database_is_empty(conn):
            raise OperationsError("RESTORE_DATABASE_NOT_EMPTY")
        for statement in SCHEMA:
            conn.execute(text(statement))
        for table, columns in TABLES.items():
            rows = snapshot[table]
            if not rows:
                continue
            names = ",".join(f'"{column}"' for column in columns)
            values = ",".join(f":v{index}" for index in range(len(columns)))
            parameters = [
                {f"v{index}": value for index, value in enumerate(row)} for row in rows
            ]
            conn.execute(text(f'INSERT INTO "{table}" ({names}) VALUES ({values})'), parameters)
        version = conn.execute(text("SELECT version FROM schema_version")).scalar_one()
        if version != 1 or _manifest_objects(conn) != expected_objects:
            raise OperationsError("RESTORED_DATABASE_INTEGRITY_FAILED")


def restore_backup(
    db,
    objects,
    source: Path,
    *,
    workers: int = 8,
    max_age_hours: float = 24,
) -> dict[str, Any]:
    if not math.isfinite(max_age_hours) or max_age_hours <= 0:
        raise OperationsError("BACKUP_AGE_LIMIT_INVALID")
    source = source.resolve(strict=True)
    manifest = _load_manifest(source, max_age_hours)
    snapshot = _load_database_snapshot(source, manifest)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda item: _verify_backup_file(source, item), manifest["objects"]))
    with db.engine.connect() as conn:
        if not _database_is_empty(conn):
            raise OperationsError("RESTORE_DATABASE_NOT_EMPTY")
    created_bucket = objects.ensure_bucket()
    if not objects.is_empty():
        raise OperationsError("RESTORE_BUCKET_NOT_EMPTY")
    uploaded: list[str] = []
    database_restored = False
    try:
        failure = None
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_upload_and_verify, objects, source, item)
                for item in manifest["objects"]
            ]
            for future in as_completed(futures):
                try:
                    uploaded.append(future.result())
                except BaseException as error:
                    failure = failure or error
        if failure is not None:
            raise failure
        _restore_database(db, snapshot, manifest["objects"])
        database_restored = True
    except BaseException:
        if not database_restored:
            try:
                objects.delete_many(uploaded)
            except BaseException:
                pass
            if created_bucket:
                try:
                    objects.delete_bucket()
                except BaseException:
                    pass
        raise
    return {
        "status": "RESTORE_COMPLETE",
        "backup_age_seconds": manifest["age_seconds"],
        "object_count": manifest["object_count"],
        "object_bytes": manifest["object_bytes"],
        "verified_objects": len(manifest["objects"]),
    }
