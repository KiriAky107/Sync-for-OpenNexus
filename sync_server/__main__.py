"""运维入口通过终端隐式输入密码，不接受命令行秘密。"""

import argparse
import getpass
import os
from pathlib import Path

from .app import create_app
from .database import Database
from .storage import S3Objects


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "migrate", "create-user"])
    parser.add_argument("--username")
    args = parser.parse_args()
    url = os.environ["SYNC_DATABASE_URL"]
    if not url.startswith("postgresql+psycopg://"):
        raise SystemExit("生产入口只支持 PostgreSQL")
    db = Database(url)
    db.migrate()
    if args.command == "create-user":
        db.add_user(args.username or input("用户名: "), getpass.getpass("密码（至少12字符）: "))
    elif args.command == "serve":
        import uvicorn
        app = create_app(db, S3Objects(os.environ["SYNC_S3_ENDPOINT"], os.environ["SYNC_S3_BUCKET"]), Path(os.environ["SYNC_STAGING_DIR"]))
        uvicorn.run(app, host="0.0.0.0", port=8080, access_log=False)


if __name__ == "__main__":
    main()
