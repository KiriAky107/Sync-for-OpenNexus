"""运维入口通过终端隐式输入密码，不接受命令行秘密。"""

import argparse
import getpass
import json
import os
from pathlib import Path
import time

from .app import create_app
from .database import Database
from .storage import S3Objects


def application():
    url = os.environ["SYNC_DATABASE_URL"]
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("生产入口只支持 PostgreSQL")
    return create_app(Database(url), S3Objects(os.environ["SYNC_S3_ENDPOINT"], os.environ["SYNC_S3_BUCKET"]),
                      Path(os.environ["SYNC_STAGING_DIR"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["serve", "initialize", "migrate", "create-user", "cleanup-uploads", "backup", "restore"],
    )
    parser.add_argument("--workers", type=int, choices=[1, 2], default=2)
    parser.add_argument("--username")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--io-workers", type=int, choices=range(1, 33), default=8)
    parser.add_argument("--max-age-hours", type=float, default=24)
    args = parser.parse_args()
    url = os.environ["SYNC_DATABASE_URL"]
    if not url.startswith("postgresql+psycopg://"):
        raise SystemExit("生产入口只支持 PostgreSQL")
    db = Database(url)
    if args.command == "initialize":
        objects = S3Objects(os.environ["SYNC_S3_ENDPOINT"], os.environ["SYNC_S3_BUCKET"])
        deadline = time.monotonic() + 60
        while True:
            try:
                db.migrate()
                created = objects.ensure_bucket()
                print(json.dumps({"schema": 1, "bucket_created": created}))
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
    elif args.command == "backup":
        if args.directory is None:
            raise SystemExit("backup 需要 --directory")
        from .operations import create_backup

        objects = S3Objects(os.environ["SYNC_S3_ENDPOINT"], os.environ["SYNC_S3_BUCKET"])
        print(
            json.dumps(
                create_backup(db, objects, args.directory, workers=args.io_workers)
            )
        )
    elif args.command == "restore":
        if args.directory is None:
            raise SystemExit("restore 需要 --directory")
        from .operations import restore_backup

        objects = S3Objects(os.environ["SYNC_S3_ENDPOINT"], os.environ["SYNC_S3_BUCKET"])
        print(
            json.dumps(
                restore_backup(
                    db,
                    objects,
                    args.directory,
                    workers=args.io_workers,
                    max_age_hours=args.max_age_hours,
                )
            )
        )
    elif args.command == "create-user":
        db.migrate()
        db.add_user(args.username or input("用户名: "), getpass.getpass("密码（至少12字符）: "))
    elif args.command == "serve":
        db.migrate()
        bootstrap = db.prepare_bootstrap_user()
        if bootstrap:
            print(json.dumps({
                "event": "SYNC_BOOTSTRAP_CREDENTIALS",
                "username": bootstrap["username"],
                "password": bootstrap["password"],
                "must_change_credentials": True,
            }), flush=True)
        import uvicorn
        host = os.environ.get("SYNC_HOST", "0.0.0.0")
        if host not in {"0.0.0.0", "127.0.0.1", "::1"}:
            raise SystemExit("SYNC_HOST 仅允许通配或本机回环地址")
        try:
            port = int(os.environ.get("SYNC_PORT", "8080"))
        except ValueError as error:
            raise SystemExit("SYNC_PORT 必须为有效端口") from error
        if not 1 <= port <= 65535:
            raise SystemExit("SYNC_PORT 必须为有效端口")
        uvicorn.run("sync_server.__main__:application", factory=True, workers=args.workers,
                    host=host, port=port, access_log=False)
    elif args.command == "cleanup-uploads":
        db.migrate()
        from .maintenance import cleanup_expired_uploads
        print(cleanup_expired_uploads(db, Path(os.environ["SYNC_STAGING_DIR"])))
    elif args.command == "migrate":
        db.migrate()


if __name__ == "__main__":
    main()
