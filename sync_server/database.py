"""Schema v1 与事务边界；生产使用 PostgreSQL，SQLite 仅用于受控协议测试。"""

from contextlib import contextmanager
import hashlib
import secrets
from sqlalchemy import create_engine, text

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, refresh TEXT UNIQUE NOT NULL, device_id TEXT NOT NULL, expires BIGINT NOT NULL, refresh_expires BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS vaults (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, sequence BIGINT NOT NULL DEFAULT 0, quota BIGINT NOT NULL, used BIGINT NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, device_id TEXT NOT NULL, hash TEXT NOT NULL, size BIGINT NOT NULL, offset_bytes BIGINT NOT NULL DEFAULT 0, expires BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS objects (vault_id TEXT NOT NULL, hash TEXT NOT NULL, size BIGINT NOT NULL, created BIGINT NOT NULL, PRIMARY KEY(vault_id, hash))",
    "CREATE TABLE IF NOT EXISTS revisions (vault_id TEXT NOT NULL, sequence BIGINT NOT NULL, file_id TEXT NOT NULL, base_revision BIGINT NOT NULL, path TEXT NOT NULL, path_key TEXT NOT NULL, operation TEXT NOT NULL, hash TEXT, size BIGINT NOT NULL, device_id TEXT NOT NULL, operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(vault_id, sequence), UNIQUE(vault_id, operation_id))",
    "CREATE TABLE IF NOT EXISTS files (vault_id TEXT NOT NULL, file_id TEXT NOT NULL, sequence BIGINT NOT NULL, path_key TEXT NOT NULL, deleted INTEGER NOT NULL, PRIMARY KEY(vault_id, file_id))",
    "CREATE TABLE IF NOT EXISTS login_limits (key TEXT PRIMARY KEY, started BIGINT NOT NULL, attempts INTEGER NOT NULL)",
]


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ":" + result.hex()


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(url)
        self.sqlite = self.engine.dialect.name == "sqlite"

    def migrate(self):
        with self.transaction() as conn:
            conn.execute(text(SCHEMA[0]))
            version = conn.execute(text("SELECT version FROM schema_version")).scalar()
            if version not in {None, 1}:
                raise RuntimeError("数据库版本不兼容，禁止写入")
            for statement in SCHEMA[1:]:
                conn.execute(text(statement))
            if version is None:
                conn.execute(text("INSERT INTO schema_version VALUES (1)"))

    @contextmanager
    def transaction(self):
        with self.engine.connect() as conn:
            try:
                if self.sqlite:
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def add_user(self, username: str, password: str):
        if len(password) < 12:
            raise ValueError("密码至少 12 字符")
        with self.transaction() as conn:
            conn.execute(text("INSERT INTO users VALUES (:id,:name,:password)"),
                         {"id": secrets.token_hex(16), "name": username, "password": password_hash(password)})


def row(conn, sql, **params):
    return conn.execute(text(sql), params).mappings().first()


def rows(conn, sql, **params):
    return conn.execute(text(sql), params).mappings().all()


def run(conn, sql, **params):
    return conn.execute(text(sql), params)
