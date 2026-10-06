"""Durable, bounded operational receipts; no paths, credentials or database dumps."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time
import uuid

from .operations import OperationsError

KINDS = {"backup", "verify", "restore"}
ERRORS = {
    "OBJECT_ID_INVALID", "OBJECT_INTEGRITY_FAILED", "HISTORICAL_OBJECT_MISSING",
    "BACKUP_PENDING_UPLOADS", "BACKUP_DESTINATION_EXISTS", "SCHEMA_INCOMPATIBLE",
    "BACKUP_MANIFEST_INVALID", "BACKUP_AGE_INVALID", "BACKUP_DATABASE_INTEGRITY_FAILED",
    "BACKUP_DATABASE_INVALID", "BACKUP_OBJECT_MISSING", "BACKUP_OBJECT_INTEGRITY_FAILED",
    "RESTORED_OBJECT_INTEGRITY_FAILED", "RESTORE_DATABASE_NOT_EMPTY",
    "RESTORED_DATABASE_INTEGRITY_FAILED", "BACKUP_AGE_LIMIT_INVALID",
    "BACKUP_WORKER_LIMIT_INVALID", "RESTORE_BUCKET_NOT_EMPTY",
    "BACKUP_FAILED", "VERIFY_FAILED", "RESTORE_FAILED", "OPERATION_INTERRUPTED",
}
SUCCESS = {"backup": "BACKUP_COMPLETE", "verify": "BACKUP_VERIFIED", "restore": "RESTORE_COMPLETE"}
COUNTS = ("object_count", "object_bytes", "verified_objects", "backup_age_seconds")
SCHEMA = """CREATE TABLE IF NOT EXISTS operation_receipts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL, state TEXT NOT NULL, code TEXT NOT NULL,
    started_at INTEGER NOT NULL, finished_at INTEGER, object_count INTEGER,
    object_bytes INTEGER, verified_objects INTEGER, backup_age_seconds INTEGER
)"""

def _utc(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat() if value is not None else None

class OperationJournal:
    def __init__(self, path: Path):
        self.path = path.resolve(strict=False)

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise OperationsError("OPERATIONS_SCHEMA_INCOMPATIBLE")
            with conn:
                conn.execute(SCHEMA)
                conn.execute("PRAGMA user_version=1")
            return conn
        except BaseException:
            conn.close()
            raise

    def start(self, kind: str) -> str:
        if kind not in KINDS:
            raise ValueError("Unknown operation kind")
        operation_id = uuid.uuid4().hex
        with closing(self._write()) as conn, conn:
            conn.execute("INSERT INTO operation_receipts(id,kind,state,code,started_at) VALUES (?,?, 'unfinished','OPERATION_UNFINISHED',?)",
                         (operation_id, kind, time.time_ns() // 1_000_000))
        return operation_id

    def finish(self, operation_id: str, kind: str, result=None, error=None):
        if kind not in KINDS:
            raise ValueError("Unknown operation kind")
        state = "failed" if error is not None else "succeeded"
        code = SUCCESS[kind]
        counts = [None] * len(COUNTS)
        if error is not None:
            code = str(error) if isinstance(error, OperationsError) and str(error) in ERRORS else kind.upper() + "_FAILED"
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                code = "OPERATION_INTERRUPTED"
        else:
            if not isinstance(result, dict) or result.get("status") != code:
                raise OperationsError("OPERATIONS_RESULT_INVALID")
            for index, name in enumerate(COUNTS):
                value = result.get(name)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1):
                    raise OperationsError("OPERATIONS_RESULT_INVALID")
                counts[index] = value
        with closing(self._write()) as conn, conn:
            changed = conn.execute("UPDATE operation_receipts SET state=?,code=?,finished_at=?,object_count=?,object_bytes=?,verified_objects=?,backup_age_seconds=? WHERE id=? AND kind=? AND state='unfinished'",
                                   (state, code, time.time_ns() // 1_000_000, *counts, operation_id, kind)).rowcount
            if changed != 1:
                raise OperationsError("OPERATIONS_RECEIPT_CHANGED")

    def run(self, kind, action):
        operation_id = self.start(kind)
        try:
            result = action()
        except BaseException as error:
            try:
                self.finish(operation_id, kind, error=error)
            except BaseException as receipt_error:
                raise OperationsError("OPERATIONS_RECEIPT_UNAVAILABLE") from receipt_error
            raise
        try:
            self.finish(operation_id, kind, result=result)
        except BaseException as receipt_error:
            # The action may have completed. Keep the original intent; never replay it.
            raise OperationsError("OPERATIONS_RECEIPT_UNAVAILABLE") from receipt_error
        return {**result, "operation_id": operation_id}

    def records(self, *, limit=20, before=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100 or (before is not None and (isinstance(before, bool) or not isinstance(before, int) or not 1 <= before <= 2**63 - 1)):
            raise ValueError("Invalid operation page")
        if not self.path.exists():
            return {"schema": 1, "items": [], "next_before": None}
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=.5)) as conn:
                conn.row_factory = sqlite3.Row
                if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise OperationsError("OPERATIONS_SCHEMA_INCOMPATIBLE")
                rows = conn.execute("SELECT * FROM operation_receipts WHERE sequence < ? ORDER BY sequence DESC LIMIT ?",
                                    (before if before is not None else 2**63 - 1, limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                kind, state, code = row["kind"], row["state"], row["code"]
                valid = kind in KINDS and state in {"unfinished", "succeeded", "failed"} and (
                    code == "OPERATION_UNFINISHED" if state == "unfinished" else code == SUCCESS[kind] if state == "succeeded" else code in ERRORS)
                if not valid or not isinstance(row["id"], str) or len(row["id"]) != 32 or any(char not in '0123456789abcdef' for char in row["id"]):
                    raise OperationsError("OPERATIONS_RECORD_INVALID")
                if any(value is not None and (not isinstance(value, int) or not 0 <= value <= 253402300799999) for value in (row["started_at"], row["finished_at"])) or row["started_at"] is None or (row["finished_at"] is None) != (state == "unfinished"):
                    raise OperationsError("OPERATIONS_RECORD_INVALID")
                item = {"sequence": row["sequence"], "operation_id": row["id"], "kind": kind, "state": state, "code": code,
                        "started_utc": _utc(row["started_at"]), "finished_utc": _utc(row["finished_at"])}
                for name in COUNTS:
                    value = row[name]
                    if value is not None and (not isinstance(value, int) or not 0 <= value <= 2**63 - 1):
                        raise OperationsError("OPERATIONS_RECORD_INVALID")
                    item[name] = value
                items.append(item)
            return {"schema": 1, "items": items, "next_before": items[-1]["sequence"] if len(rows) > limit else None}
        except (sqlite3.Error, OSError, ValueError, OverflowError) as error:
            raise OperationsError("OPERATIONS_STATUS_UNAVAILABLE") from error
