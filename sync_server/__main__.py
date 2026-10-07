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


def operations_path():
    return Path(os.environ.get("SYNC_OPERATIONS_PATH", str(Path(os.environ.get("SYNC_STAGING_DIR", ".")) / "operations.sqlite3")))


def application():
    url = os.environ["SYNC_DATABASE_URL"]
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("生产入口只支持 PostgreSQL")
    return create_app(Database(url), S3Objects(os.environ["SYNC_S3_ENDPOINT"], os.environ["SYNC_S3_BUCKET"]),
                      Path(os.environ["SYNC_STAGING_DIR"]), operations_path=operations_path(),
                      operator_user_ids=tuple(value.strip() for value in os.environ.get("SYNC_OPERATOR_USER_IDS", "").split(",") if value.strip()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["serve", "initialize", "migrate", "create-user", "bootstrap-user", "cleanup-uploads", "backup", "verify-backup", "restore", "operation-records", "operator-id", "gc-preview", "gc-apply", "gc-status"],
    )
    parser.add_argument("--workers", type=int, choices=[1, 2], default=2)
    parser.add_argument("--username")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--io-workers", type=int, choices=range(1, 33), default=8)
    parser.add_argument("--max-age-hours", type=float, default=24)
    parser.add_argument("--limit", type=int, choices=range(1, 101), default=20)
    parser.add_argument("--before", type=int)
    parser.add_argument("--vault-id")
    parser.add_argument("--plan-id")
    parser.add_argument("--confirm-plan")
    parser.add_argument("--grace-hours", type=float, default=168)
    args = parser.parse_args()
    from .operation_journal import OperationJournal
    journal = OperationJournal(operations_path())
    if args.command == "operation-records":
        print(json.dumps(journal.records(limit=args.limit, before=args.before)))
        return
    if args.command == "verify-backup":
        if args.directory is None:
            raise SystemExit("verify-backup needs --directory")
        from .operations import verify_backup
        print(json.dumps(journal.run("verify", lambda: verify_backup(args.directory, workers=args.io_workers, max_age_hours=args.max_age_hours))))
        return
    url = os.environ["SYNC_DATABASE_URL"]
    if not url.startswith("postgresql+psycopg://"):
        raise SystemExit("生产入口只支持 PostgreSQL")
    db = Database(url)
    if args.command in {'gc-preview', 'gc-apply', 'gc-status'}:
        from .reclamation import apply, preview, status
        if args.command != 'gc-status':
            db.migrate()
        if args.command == 'gc-status':
            if not args.plan_id:
                raise SystemExit('gc-status needs --plan-id')
            result = status(db, args.plan_id)
        else:
            if args.directory is None:
                raise SystemExit('gc-preview and gc-apply need --directory')
            if args.command == 'gc-preview':
                if not args.vault_id:
                    raise SystemExit('gc-preview needs --vault-id')
                result = preview(db, args.directory, args.vault_id, grace_hours=args.grace_hours,
                                 limit=args.limit, max_age_hours=args.max_age_hours, workers=args.io_workers)
            else:
                if not args.plan_id or not args.confirm_plan:
                    raise SystemExit('gc-apply needs --plan-id and --confirm-plan')
                objects = S3Objects(os.environ['SYNC_S3_ENDPOINT'], os.environ['SYNC_S3_BUCKET'])
                result = apply(db, objects, args.directory, args.plan_id, confirm_plan=args.confirm_plan,
                               max_age_hours=args.max_age_hours, workers=args.io_workers)
        print(json.dumps(result))
    elif args.command == "operator-id":
        if not args.username:
            raise SystemExit("operator-id needs --username")
        from .database import row
        with db.transaction() as conn:
            account = row(conn, "SELECT id FROM users WHERE username=:name", name=args.username)
            if not account:
                raise SystemExit("ACCOUNT_NOT_FOUND")
            print(json.dumps({"user_id": account["id"]}))
    elif args.command == "initialize":
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
                journal.run("backup", lambda: create_backup(db, objects, args.directory, workers=args.io_workers))
            )
        )
    elif args.command == "restore":
        if args.directory is None:
            raise SystemExit("restore 需要 --directory")
        from .operations import restore_backup

        objects = S3Objects(os.environ["SYNC_S3_ENDPOINT"], os.environ["SYNC_S3_BUCKET"])
        print(
            json.dumps(
                journal.run("restore", lambda: restore_backup(
                    db,
                    objects,
                    args.directory,
                    workers=args.io_workers,
                    max_age_hours=args.max_age_hours,
                ))
            )
        )
    elif args.command == "create-user":
        db.migrate()
        db.add_user(args.username or input("用户名: "), getpass.getpass("密码（至少12字符）: "))
    elif args.command == "bootstrap-user":
        db.migrate()
        bootstrap = db.prepare_bootstrap_user(force=True)
        print(json.dumps({
            "event": "SYNC_BOOTSTRAP_CREDENTIALS",
            "username": bootstrap["username"],
            "password": bootstrap["password"],
            "must_change_credentials": True,
        }), flush=True)
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
