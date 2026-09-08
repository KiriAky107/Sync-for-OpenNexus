"""Delete expired upload staging only; referenced historical objects are never GC'd."""
import re
import time
from pathlib import Path
from .database import row, rows, run


def cleanup_expired_uploads(db, staging: Path, *, now=None, limit=500):
    now = int(time.time() if now is None else now)
    with db.transaction() as conn:
        expired = rows(conn, "SELECT id,vault_id FROM uploads WHERE expires<=:now ORDER BY expires LIMIT :limit", now=now, limit=limit)
    removed = 0
    for candidate in expired:
        # Same lock order as PUT/complete. Recheck expiry after acquiring the lock.
        with db.transaction() as conn:
            suffix = " FOR UPDATE" if not db.sqlite else ""
            row(conn, "SELECT id FROM vaults WHERE id=:v" + suffix, v=candidate["vault_id"])
            upload = row(conn, "SELECT id FROM uploads WHERE id=:id AND expires<=:now", id=candidate["id"], now=now)
            if upload:
                if not re.fullmatch(r"[0-9a-f]{32}", upload["id"]):
                    raise RuntimeError("UPLOAD_ID_INVALID")
                # File first: interruption leaves an expired row that can be retried.
                (staging / upload["id"]).unlink(missing_ok=True)
                run(conn, "DELETE FROM uploads WHERE id=:id", id=upload["id"])
                removed += 1
    return {"expired_uploads_removed": removed}
