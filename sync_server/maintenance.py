"""仅删除过期的上传暂存；引用的历史对象永远不会是 GC'd。"""
import re
import time
from pathlib import Path
from .database import row, rows, run
from .usage import disposition


def cleanup_expired_uploads(db, staging: Path, *, now=None, limit=500):
    now = int(time.time() if now is None else now)
    started = time.monotonic()
    with db.transaction() as conn:
        expired = rows(conn, "SELECT id,vault_id FROM uploads WHERE expires<=:now ORDER BY expires LIMIT :limit", now=now, limit=limit)
    removed = released = filesystem_failures = metadata_failures = 0
    for candidate in expired:
        # 与 PUT 相同的锁定顺序/完成。获取锁后重新检查过期时间。
        try:
            with db.transaction() as conn:
                suffix = " FOR UPDATE" if not db.sqlite else ""
                row(conn, "SELECT id FROM vaults WHERE id=:v" + suffix, v=candidate["vault_id"])
                upload = row(conn, "SELECT * FROM uploads WHERE id=:id AND expires<=:now", id=candidate["id"], now=now)
                if upload:
                    if not re.fullmatch(r"[0-9a-f]{32}", upload["id"]):
                        raise RuntimeError("UPLOAD_ID_INVALID")
                    # File first: an interrupted or failed removal retains a retryable row.
                    (staging / upload["id"]).unlink(missing_ok=True)
                    disposition(conn, upload, 'expired', 'maintenance', now)
                    run(conn, "DELETE FROM uploads WHERE id=:id", id=upload["id"])
            if upload:
                removed += 1
                released += upload['size']
        except OSError:
            filesystem_failures += 1
        except RuntimeError:
            metadata_failures += 1
    duration = int((time.monotonic()-started)*1000)
    with db.transaction() as conn:
        run(conn, """INSERT INTO maintenance_summary VALUES
            ('uploads',:now,:finished,:duration,:selected,:removed,:released,:filesystem,:metadata,:removed,:failures)
            ON CONFLICT(kind) DO UPDATE SET started_at=:now,finished_at=:finished,duration_ms=:duration,
            selected=:selected,removed=:removed,released_bytes=:released,filesystem_failures=:filesystem,
            metadata_failures=:metadata,total_removed=maintenance_summary.total_removed+:removed,
            total_failures=maintenance_summary.total_failures+:failures""",
            now=now, finished=now+duration//1000, duration=duration, selected=len(expired), removed=removed, released=released,
            filesystem=filesystem_failures, metadata=metadata_failures, failures=filesystem_failures+metadata_failures)
    return {"expired_uploads_removed": removed, 'released_bytes':released, 'filesystem_failures':filesystem_failures, 'metadata_failures':metadata_failures, 'duration_ms':duration}
