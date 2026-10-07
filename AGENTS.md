# Repository writing guide

The canonical README style is defined in [OpenNexus's AGENTS.md](https://github.com/KiriAky107/OpenNexus/blob/main/AGENTS.md). Apply it to `README.md` and `README.zh-CN.md` here.

- Use the existing OpenNexus logo and a centered header with a short Sync description, language switch, navigation and relevant `flat-square` badges. Link version and CI badges to this repository.
- Present the published release, updates, highlights, quick start and concrete user workflows before architecture and development details. Adapt installation instructions to a self-hosted service.
- Explain account setup, desktop connection, history, uploads and recovery in terms of actual user actions. Label CLI and console features accurately; do not document an unfinished console screen as available.
- Keep English and Simplified Chinese READMEs equivalent in section order, facts, commands, examples and requirements. Keep API routes, identifiers, variable names and commands exact.
- Use short paragraphs, task-oriented headings, numbered steps for workflows, tables for configuration and Mermaid for useful architecture or protocol diagrams. Use screenshots only from the real application.
- Make capability descriptions concrete. Avoid slogans, filler, exaggerated readiness claims and repetitive disclaimers. Keep product versions distinct from Sync protocol versions.
- Preserve the documented TLS, account, revision, object-integrity, quota and backup behavior. Describe transport encryption accurately; only claim end-to-end encryption after production implementation and verification.
- Verify quick-start commands and links against the current implementation. Keep credentials as placeholders and show the applicable restore target requirements.

Private plans, internal `docs`, local receipts, credentials, databases, object bytes, backups and user vaults stay local. Public README files and this user-requested writing guide may be committed. Do not repeat unchanged documentation tracking checks. Group changes by purpose, validate affected behavior, and protect production data and old releases.
