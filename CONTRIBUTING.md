# Contributing

## Scope

This repository contains the Sync Server, management console, deployment templates, protocol tests, and operational tools. Desktop AI Core changes belong in [OpenNexus](https://github.com/KiriAky107/OpenNexus); catalog and package-policy changes belong in [Community for OpenNexus](https://github.com/KiriAky107/Community-for-OpenNexus).

## Workflow

1. Search existing Issues and open one for protocol, schema, security, compatibility, or operational changes.
2. Create a focused feature branch from current `main`.
3. Use Conventional Commits and keep accurate authorship.
4. Add tests and update both README languages for public behavior changes.
5. Open a Pull Request using the repository template.

## Engineering requirements

- Preserve account and Vault isolation, revision ordering, idempotency, quota accounting, and immutable-object integrity.
- Add schema changes through an explicit versioned migration and document backup, restore, upgrade, and rollback effects.
- Never weaken TLS, path validation, content hashes, upload bounds, authorization, device revocation, or production storage checks for convenience.
- Keep production PostgreSQL/S3 behavior covered; SQLite and local disk tests are not production evidence.
- Explain dependency licenses, container size, network exposure, and operational impact.
- Do not commit `.env`, deployment credentials, dumps, object data, backups, account details, private hosts, or user Vault content.

## Required verification

```powershell
uv sync --frozen
uv run pytest

cd console
pnpm install --frozen-lockfile
pnpm type-check
pnpm build
```

List only checks actually run. Protocol or storage changes should include PostgreSQL/S3 acceptance; deployment changes should include health, readiness, bootstrap credential, upgrade, backup, and restore evidence as applicable.

Participation follows the [Code of Conduct](CODE_OF_CONDUCT.md). Vulnerabilities follow [SECURITY.md](SECURITY.md).
