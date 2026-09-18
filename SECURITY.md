# Security Policy

## Supported versions

Security fixes target the latest release and current `main`. Earlier alpha versions do not receive guaranteed backports.

## Private reporting

Do not open a public Issue for an unpatched vulnerability, leaked credential, private deployment detail, or report containing user data. Use GitHub Private Vulnerability Reporting when enabled, or contact the repository owner privately through a method published on the owner's GitHub profile.

Include the affected version, route or component, prerequisites, minimum reproduction, expected impact, sanitized evidence, and possible mitigation. Never send real passwords, tokens, database dumps, object data, backups, private hostnames, or Vault content.

Important security boundaries include account/Vault authorization, sessions and device revocation, upload limits and offsets, path normalization, content hashes, revision conflicts, quota accounting, backup confidentiality, PostgreSQL/S3 credentials, TLS termination, and container hardening.

Maintainers will attempt to reproduce, assess, fix, test, and coordinate disclosure. Alpha development has no guaranteed response SLA.
