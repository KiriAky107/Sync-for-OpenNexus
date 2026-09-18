# Sync for OpenNexus

[简体中文](README.zh-CN.md) | **English**

[![Version](https://img.shields.io/badge/version-0.5.2--alpha1-5865f2)](https://github.com/KiriAky107/Sync-for-OpenNexus/releases/tag/v0.5.2-alpha1)
![Python](https://img.shields.io/badge/Python-3.12%2B-3776ab)
![API](https://img.shields.io/badge/API-FastAPI-05998b)
![Console](https://img.shields.io/badge/console-Vue%203-42b883)
[![License](https://img.shields.io/badge/license-MIT-22c55e)](LICENSE)

Sync for OpenNexus is the optional self-hosted synchronization service for OpenNexus vaults. It manages accounts, devices, immutable content objects, ordered file revisions, resumable uploads, backup, and empty-instance restoration without running the desktop AI Core or reading a user's local vault directly.

> **Alpha status:** deploy behind TLS and access controls. PostgreSQL and S3-compatible object storage are the production path; SQLite and direct HTTP are limited to isolated tests.

## Capabilities and boundaries

- Access/refresh sessions and per-device revocation.
- Per-user Vault isolation, quotas, and ordered revision streams.
- Resumable chunk uploads with offset, size, SHA-256, and idempotent completion checks.
- Immutable object storage separated from PostgreSQL metadata.
- Conflict-safe base revisions consumed by the OpenNexus desktop outbox/inbox client.
- Repeatable-read backup and verified restoration into an empty deployment.
- Same-origin Vue management console for health, account, Vault, and device operations.

The server does not run models, index Markdown, install extensions, or accept OpenNexus provider credentials. It does not reuse Community tokens.

## Architecture

```mermaid
flowchart LR
    Desktop[OpenNexus desktop clients] -->|HTTPS Sync v1| Proxy[TLS reverse proxy]
    Console[Vue 3 Sync Console] -->|same-origin API| Proxy
    Proxy --> API[FastAPI Sync service]
    API --> PG[(PostgreSQL 17 metadata)]
    API --> Stage[(bounded staging volume)]
    API --> S3[(S3-compatible object storage)]
    Init[one-shot initialize job] --> PG
    Init --> S3
    Backup[backup and restore CLI] --> PG
    Backup --> S3
```

`compose.yaml` binds the API to `127.0.0.1:8080`, runs a one-shot idempotent initializer, drops Linux capabilities, uses a read-only service filesystem, and gives the long-running service bucket-scoped credentials instead of MinIO root credentials.

## Sync protocol flow

```mermaid
sequenceDiagram
    autonumber
    participant Client as Desktop client
    participant API as Sync v1 API
    participant DB as PostgreSQL
    participant Obj as Object storage

    Client->>API: Handshake and authenticate device
    API->>DB: Create access and refresh session
    Client->>API: Create resumable upload with hash and size
    API->>DB: Reserve upload and quota
    loop bounded chunks
        Client->>API: PUT chunk at expected offset
        API->>DB: Persist new offset
    end
    Client->>API: Complete upload
    API->>Obj: Store immutable object
    API->>DB: Write object row and completion receipt
    Client->>API: Commit revision with base revision and operation ID
    API->>DB: Validate ownership, path, sequence and idempotency
    alt base revision accepted
        API->>DB: Append revision and update file head
        API-->>Client: New ordered sequence
    else conflict
        API-->>Client: Conflict with current revision
    end
    Client->>API: Poll changes after cursor
    API-->>Client: Ordered revisions and object hashes
    Client->>API: Download required immutable objects
```

## Database model

```mermaid
erDiagram
    USERS ||--o{ DEVICES : owns
    DEVICES ||--o{ SESSIONS : authenticates
    USERS ||--o{ VAULTS : owns
    VAULTS ||--o{ UPLOADS : stages
    DEVICES ||--o{ UPLOADS : creates
    VAULTS ||--o{ OBJECTS : stores
    VAULTS ||--o{ REVISIONS : appends
    DEVICES ||--o{ REVISIONS : submits
    VAULTS ||--o{ FILES : tracks
    UPLOADS ||--o| UPLOAD_RECEIPTS : completes_as

    USERS {
        string id PK
        string username UK
        string password_hash
    }
    DEVICES {
        string id PK
        string user_id FK
        string name
        bool revoked
    }
    SESSIONS {
        string token PK
        string refresh UK
        string device_id FK
        int expires
        int refresh_expires
    }
    VAULTS {
        string id PK
        string user_id FK
        string name
        int sequence
        int quota
        int used
    }
    UPLOADS {
        string id PK
        string vault_id FK
        string device_id FK
        string hash
        int size
        int offset_bytes
        int expires
    }
    OBJECTS {
        string vault_id PK, FK
        string hash PK
        int size
        int created
    }
    REVISIONS {
        string vault_id PK, FK
        int sequence PK
        string file_id
        int base_revision
        string path
        string operation
        string hash
        string device_id FK
        string operation_id UK
    }
    FILES {
        string vault_id PK, FK
        string file_id PK
        int sequence
        string path_key
        bool deleted
    }
    UPLOAD_RECEIPTS {
        string id PK
        string vault_id FK
        string device_id FK
        string hash
        int completed
    }
```

The schema also contains `schema_version`, `login_limits`, and `bootstrap_state`. Object bytes live in S3; PostgreSQL remains the authority for ownership, revision ordering, quota accounting, receipts, and object inventory.

## Repository layout

| Path | Purpose |
| --- | --- |
| `sync_server/` | API, protocol, database, storage, readiness, backup, and restore |
| `console/` | Vue 3 and TypeScript management console |
| `tests/` | Protocol, production storage, readiness, console, and benchmark tests |
| `tools/` | Bounded upload and transfer probes |
| `compose.yaml` | PostgreSQL, MinIO, initializer, and hardened Sync service |
| `compose.test.yaml` | Explicit isolated-test overrides |

## Quick start

```powershell
Copy-Item .env.example .env
# Generate independent values for every blank secret, then edit .env.
docker compose up -d --build
docker compose ps
docker compose logs sync
```

The first empty database creates a temporary `admin` account and emits `SYNC_BOOTSTRAP_CREDENTIALS` to the Sync container log. Sign in once and immediately change both username and password. Until fixed, restart rotates the bootstrap password and revokes old sessions.

### Required deployment variables

| Variable | Purpose |
| --- | --- |
| `POSTGRES_PASSWORD` | PostgreSQL container password |
| `SYNC_DATABASE_URL` | URL-encoded PostgreSQL SQLAlchemy URL |
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | Initializer-only object-store administration |
| `SYNC_ACCESS_KEY_ID`, `SYNC_SECRET_ACCESS_KEY` | Bucket-scoped runtime identity |
| `SYNC_S3_BUCKET` | Existing or initialized object bucket |
| `SYNC_BIND_ADDRESS`, `SYNC_PORT` | Host bind address and port |

Never reuse MinIO root credentials as runtime credentials. Keep the bucket name stable during upgrades.

## Health and operations

- `/health` reports process health.
- `/ready` verifies database schema, staging writes, and object-store probe operations.
- `/` and `/console/` serve the same-origin management console.
- Production traffic must terminate TLS at a reverse proxy; `Caddyfile.example` is a starting point.
- Direct exposure through `compose.test.yaml` is only for an authorized isolated demonstration environment.

## Backup and restore

```powershell
python -m sync_server backup --directory D:/OpenNexus-backups/latest --io-workers 8
python -m sync_server restore --directory D:/OpenNexus-backups/latest --io-workers 8
```

```mermaid
flowchart LR
    A[Begin repeatable-read transaction] --> B[Export schema v1 rows]
    B --> C[Build immutable object manifest]
    C --> D{Uploads incomplete or object missing?}
    D -- Yes --> X[Fail backup without publishing partial result]
    D -- No --> E[Stream objects and verify size plus SHA-256]
    E --> F[Write completed backup metadata]
    F --> G{Restore target DB and bucket empty?}
    G -- No --> Y[Refuse restore]
    G -- Yes --> H[Validate complete backup and age policy]
    H --> I[Upload and read back all objects]
    I --> J[Import rows in one PostgreSQL transaction]
    J --> K[Start service and verify readiness]
```

Backup directories contain authentication and session hashes. Protect them with restricted ACLs, encrypted storage, retention rules, and off-host copies. Practice restoration on a separate empty instance.

## Development and tests

```powershell
uv sync --frozen
uv run pytest

cd console
corepack enable
corepack prepare pnpm@10.28.0 --activate
pnpm install --frozen-lockfile
pnpm type-check
pnpm build
```

Tests create temporary databases, object directories, and staging directories; they must not read an OpenNexus user Vault or real deployment credentials.

## Security and community

- Do not commit `.env`, tokens, passwords, dumps, object data, backups, or user Vault content.
- Restrict database and object-store networks, enable monitoring, and revoke lost devices.
- Treat path normalization, operation IDs, base revisions, quotas, content hashes, and account boundaries as security controls.
- Report vulnerabilities according to [SECURITY.md](SECURITY.md), not in a public Issue.
- Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).
- Use the repository Issue forms and Pull Request template; redact infrastructure and account details.

Related repositories: [OpenNexus](https://github.com/KiriAky107/OpenNexus) and [Community for OpenNexus](https://github.com/KiriAky107/Community-for-OpenNexus).

## License

This project is licensed under the [MIT License](LICENSE). Third-party components remain subject to their own licenses and notices.
