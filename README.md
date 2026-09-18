# Sync for OpenNexus

[简体中文](README.zh-CN.md) | **English**

![Version](https://img.shields.io/badge/version-0.5.2--alpha1-5865f2)
![Python](https://img.shields.io/badge/Python-3.12%2B-3776ab)
![Console](https://img.shields.io/badge/console-Vue%203-42b883)
![License](https://img.shields.io/badge/license-MIT-green)

Sync for OpenNexus is the optional self-hosted synchronization service for OpenNexus vaults, notes, attachments, revisions, and registered devices. It is deployed independently from the desktop AI Core and does not contain user vaults, account databases, object-storage data, or deployment secrets.

## Components

- `sync_server/` — Sync v1 API, authentication, revision handling, device management, backup, and restore.
- `console/` — Vue 3 and TypeScript management console served from `/` and `/console/`.
- `tests/` — isolated API, storage, backup, restore, and security tests.
- `tools/` — bounded deployment and transfer probes.

Production deployments use PostgreSQL and S3-compatible object storage. SQLite and direct HTTP options are intended only for isolated tests.

## Quick start

```powershell
Copy-Item .env.example .env
# Replace every example secret before starting the service.
docker compose up -d --build
```

The default production compose configuration binds to `127.0.0.1:8080`. Put the service behind a TLS reverse proxy, use independent database and object-storage credentials, and back up both PostgreSQL and the object bucket.

For an isolated demonstration environment only:

```powershell
docker compose -f compose.yaml -f compose.test.yaml up -d --build
```

## Development

```powershell
uv sync --frozen
uv run pytest

cd console
pnpm install --frozen-lockfile
pnpm build
```

Tests create temporary databases, object directories, and staging directories; they do not read an OpenNexus user vault.

## First account

A new database creates a temporary `admin` account with a random bootstrap password. Read the `SYNC_BOOTSTRAP_CREDENTIALS` entry from `docker compose logs sync`, sign in once, and immediately replace both the username and password. Until credentials are fixed, a restart rotates the password and revokes old sessions.

## Backup and restore

```powershell
python -m sync_server backup --directory D:/OpenNexus-backups/latest --io-workers 8
python -m sync_server restore --directory D:/OpenNexus-backups/latest --io-workers 8
```

Restore accepts only a verified backup, an empty PostgreSQL database, and an empty or absent bucket. Practice restoration on a separate instance before relying on a backup policy.

## Security

- Never commit `.env`, passwords, access tokens, database dumps, vault data, or object-storage contents.
- Use TLS, network access controls, monitoring, and off-host backups in production.
- Keep MinIO root credentials separate from the bucket-scoped runtime account.
- Treat backup directories as sensitive because they contain authentication and session hashes.

The desktop application is maintained at [OpenNexus](https://github.com/KiriAky107/OpenNexus).

## License

This project is licensed under the [MIT License](LICENSE). Third-party components remain subject to their own licenses and notices.
