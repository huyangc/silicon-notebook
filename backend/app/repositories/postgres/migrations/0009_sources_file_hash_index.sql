-- Mirror SQLite v30's sources(notebook_id, file_hash) dedup lookup index so the
-- packaged PostgreSQL schema stays in one-for-one parity with the reviewed
-- SQLite contract. Backs same-notebook content dedup on both backends.

CREATE INDEX idx_sources_notebook_file_hash ON sources(notebook_id, file_hash);
