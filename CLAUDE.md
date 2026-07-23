# Claude Code Instructions

@AGENTS.md

`AGENTS.md` is the complete, authoritative repository working contract and must be loaded
before making changes. This file intentionally imports it so Claude Code receives the same
constraints rather than maintaining a divergent copy.

For the database adapter specifically: DATABASE_URL selects the formal repository backend through one repository factory.
Exactly one active repository backend is selected centrally from `DATABASE_URL`.
SQLite and PostgreSQL are both available direct backends; SQLite remains the shipped default.
PostgreSQL vectors use `bytea`; pgvector is not installed or required,
and production remains `--workers 1`. `SHADOW_DATABASE_URL` is reserved/validated only and
does not copy, migrate, synchronize, or enable dual-write. Status/readiness diagnostics must
redact credentials and options. Follow the stop/backup/change/start/`/api/ready` switch and
rollback contract in `AGENTS.md` and the READMEs. The PostgreSQL integration launcher gives
pytest only password-free URLs and transports per-target credentials through a temporary
mode-0600 `PGPASSFILE`.
