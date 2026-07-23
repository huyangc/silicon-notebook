# Claude Code Instructions

@AGENTS.md

`AGENTS.md` is the complete, authoritative repository working contract and must be loaded
before making changes. This file intentionally imports it so Claude Code receives the same
constraints rather than maintaining a divergent copy.

For the database adapter specifically: DATABASE_URL selects the formal repository backend through one repository factory.
Exactly one active repository backend is selected centrally from `DATABASE_URL`.
SQLite and PostgreSQL are both available direct backends; SQLite remains the shipped default.
Only the shipped-default SQLite quick start requires no database server; PostgreSQL requires
an accessible server, installed dependencies, and `DATABASE_URL`, and startup migrates the
selected datastore.
PostgreSQL vectors use `bytea`; pgvector is not installed or required,
and production remains `--workers 1`. `SHADOW_DATABASE_URL` is reserved/validated only and
does not copy, migrate, synchronize, or enable dual-write. Status/readiness diagnostics must
redact credentials and options. Follow the stop/backup/change/start/`/api/ready` switch and
rollback contract in `AGENTS.md` and the READMEs. The PostgreSQL integration launcher uses
a strict single-`sslmode` URL-query allowlist and gives pytest only a minimal environment
with password-free URLs. Its parent process only parses; the password-free preflight helper
and pytest reuse that exact environment and pgpass file. Exact per-target credentials
precede inherited lines in a failure-cleaned, mode-0600 temporary `PGPASSFILE`. A resource
owner exists before scoped SIGINT/SIGTERM handlers; handlers record the first pending signal
without raising in a factory-return window, and factories register pgpass/Popen resources
before a checkpoint acts. Cleanup boundedly reaps the child, removes credentials, restores
handlers, and returns 130/143; signal-terminated children map to `128+signum`.
