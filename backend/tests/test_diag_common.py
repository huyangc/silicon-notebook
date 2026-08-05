from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def load_common():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("diag_common", SCRIPTS / "diag_common.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def line(identifier, ts, latency=10):
    return json.dumps({
        "id": identifier,
        "kind": "http",
        "channel": "requests",
        "method": "GET",
        "path": "/api/notebooks/nb-secret/sources",
        "latency_ms": latency,
        "ts": ts,
    }) + "\n"


def test_reads_legacy_daily_gzip_and_per_user_once(tmp_path):
    (tmp_path / "requests.jsonl").write_text(line("legacy", "2026-07-20T09:00:00"))
    duplicate = line("daily", "2026-07-21T09:00:00", 20)
    (tmp_path / "requests-2026-07-21.jsonl").write_text(duplicate + "{broken\n")
    with gzip.open(tmp_path / "requests-2026-07-21.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(duplicate)
    user = tmp_path / "user-abc"
    user.mkdir()
    (user / "requests-2026-07-21.jsonl").write_text(line("user", "2026-07-21T10:00:00", 30))

    common = load_common()
    result = common.read_channel(
        tmp_path,
        "requests",
        since_hours=48,
        now=datetime.fromisoformat("2026-07-21T12:00:00"),
    )

    assert [row["id"] for row in result.records] == ["legacy", "daily", "user"]
    assert result.stats.files == 4
    assert result.stats.malformed == 1
    assert result.stats.duplicates == 1
    assert result.stats.retained == 3


def test_window_and_limit_keep_only_matching_newest_records(tmp_path):
    rows = [line(str(index), f"2026-07-21T{index:02d}:00:00") for index in range(10)]
    (tmp_path / "events-2026-07-21.jsonl").write_text("".join(rows))
    common = load_common()
    result = common.read_channel(
        tmp_path,
        "events",
        since_hours=4,
        limit=2,
        now=datetime.fromisoformat("2026-07-21T10:00:00"),
    )
    assert [row["id"] for row in result.records] == ["8", "9"]
    assert result.stats.matched == 4
    assert result.stats.retained == 2


def test_http_path_normalization_does_not_return_identifiers():
    common = load_common()
    value = common.normalize_http_path(
        "/api/notebooks/nb-private123/sources/src-private456?token=secret"
    )
    assert value == "/api/notebooks/{id}/sources/{id}"
    assert "private" not in value
    assert "token" not in value


def test_http_path_normalization_redacts_opaque_share_tokens():
    common = load_common()
    token = "shr-opaque-share-token-without-digits"
    value = common.normalize_http_path(f"/shared/{token}/preview")
    assert value == "/shared/{token}/preview"
    assert token not in value


def test_http_path_normalization_fails_closed_for_search_terms_and_filenames():
    common = load_common()
    term = "confidential-analogue-design"
    filename = "customer-secret-notes.pdf"
    assert common.normalize_http_path(
        f"/api/notebooks/nb-private123/search/{term}"
    ) == "/api/notebooks/{id}/search/{redacted}"
    assert common.normalize_http_path(
        f"/api/notebooks/nb-private123/sources/{filename}"
    ) == "/api/notebooks/{id}/sources/{redacted}"
    assert term not in common.normalize_http_path(f"/api/search/{term}")
    assert filename not in common.normalize_http_path(f"/api/sources/{filename}")


def test_reader_does_not_parse_a_gzip_line_larger_than_the_hard_byte_bound(tmp_path, monkeypatch):
    common = load_common()
    payload = json.dumps({"id": "oversized", "payload": "x" * 4096}) + "\n"
    with gzip.open(tmp_path / "events-2026-07-21.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(payload)
    loads = common.json.loads

    def reject_oversized(value, *args, **kwargs):
        assert len(value.encode("utf-8")) <= 128
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(common.json, "loads", reject_oversized)
    result = common.read_channel(tmp_path, "events", max_input_bytes=128)

    assert result.records == ()
    assert result.stats.truncated is True


def test_reader_checks_deadline_before_parsing_input(tmp_path, monkeypatch):
    common = load_common()
    (tmp_path / "events.jsonl").write_text(line("deadline", "2026-07-21T10:00:00"))

    def fail_if_parsed(*args, **kwargs):
        raise AssertionError("deadline must stop before JSON parsing")

    monkeypatch.setattr(common.json, "loads", fail_if_parsed)
    result = common.read_channel(tmp_path, "events", deadline=0)

    assert result.records == ()
    assert result.stats.truncated is True


def test_report_pseudonyms_are_stable_within_one_report_and_reset_between_reports(capsys):
    common = load_common()
    raw_notebook = "customerNotebookAlpha"

    def render():
        print(common.pseudonym("notebook", raw_notebook))
        print(common.pseudonym("notebook", raw_notebook))
        print(common.pseudonym("notebook", "customerNotebookBeta"))

    assert common.run_copy_safe(render) == 0
    first = capsys.readouterr().out
    assert raw_notebook not in first
    assert first.splitlines() == ["notebook#1", "notebook#1", "notebook#2"]

    assert common.run_copy_safe(
        lambda: print(common.pseudonym("notebook", raw_notebook))
    ) == 0
    assert capsys.readouterr().out.splitlines() == ["notebook#1"]


def test_database_target_follows_database_url_instead_of_assuming_sqlite(
    tmp_path, monkeypatch
):
    """Diagnostics must not answer from a stale SQLite file on a PG deployment.

    Every diag entry point used to open `.local/silicon_notebook.db`
    unconditionally.  On PostgreSQL that file is stale or empty, so the tools
    produced a confident, wrong diagnosis rather than refusing.
    """
    common = load_common()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # check.sh 全局设 SILICON_NOTEBOOK_ENV_FILE=""(不读任何 env 文件)以隔离
    # 真实凭据;本用例要验证的正是 .env 读取,所以显式解除该隔离。
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)

    # No configuration at all → SQLite, matching the shipped default.
    assert common.resolve_database_target(str(tmp_path)).is_sqlite

    # .env is the deployment's own record; the service reads it the same way.
    (tmp_path / ".env").write_text(
        "# comment\nOTHER=1\nDATABASE_URL=postgresql://user:pw@host:5432/db\n",
        encoding="utf-8",
    )
    target = common.resolve_database_target(str(tmp_path))
    assert target.is_sqlite is False
    assert target.backend == "postgres"
    assert target.source == "dotenv"
    # Never echo credentials or host back into a diagnostic report.
    rendered = target.explain() + target.skip_note()
    for secret in ("user", "pw", "host", "5432"):
        assert secret not in rendered

    # Process environment wins over .env, matching pydantic-settings.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./local.db")
    env_target = common.resolve_database_target(str(tmp_path))
    assert env_target.is_sqlite and env_target.source == "env"

    # A quoted value and a later duplicate both resolve the way dotenv does.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / ".env").write_text(
        'DATABASE_URL="sqlite:///./a.db"\nDATABASE_URL=postgresql://h/db\n',
        encoding="utf-8",
    )
    assert common.resolve_database_target(str(tmp_path)).backend == "postgres"


def test_database_target_reads_the_exported_dotenv_form(tmp_path, monkeypatch):
    """`export DATABASE_URL=...` is a supported form and must not fall back.

    The migration activation path writes it and the application's dotenv loader
    accepts it.  Treating `export DATABASE_URL` as the key name resolves to
    SQLite, which is the exact stale-database misread this resolver prevents.
    """
    common = load_common()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # check.sh 全局设 SILICON_NOTEBOOK_ENV_FILE=""(不读任何 env 文件)以隔离
    # 真实凭据;本用例要验证的正是 .env 读取,所以显式解除该隔离。
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)

    for line in (
        "export DATABASE_URL=postgresql://h/db",
        "export  DATABASE_URL=postgresql://h/db",
        'export DATABASE_URL="postgresql://h/db"',
    ):
        (tmp_path / ".env").write_text(line + "\n", encoding="utf-8")
        target = common.resolve_database_target(str(tmp_path))
        assert target.backend == "postgres", line
        assert target.is_sqlite is False, line

    # A key that merely starts with the same letters is still not a match.
    (tmp_path / ".env").write_text("exported_DATABASE_URL=postgresql://h/db\n",
                                   encoding="utf-8")
    assert common.resolve_database_target(str(tmp_path)).is_sqlite


def test_database_target_honors_the_env_file_override(tmp_path, monkeypatch):
    """`SILICON_NOTEBOOK_ENV_FILE` moves the file the application actually reads."""
    common = load_common()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / ".env").write_text("DATABASE_URL=sqlite:///./a.db\n", encoding="utf-8")
    elsewhere = tmp_path / "custom.env"
    elsewhere.write_text("DATABASE_URL=postgresql://h/db\n", encoding="utf-8")

    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", str(elsewhere))
    assert common.resolve_database_target(str(tmp_path)).backend == "postgres"

    # Empty override means "read no env file", exactly as app.core.config does.
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    assert common.resolve_database_target(str(tmp_path)).source == "default"

    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE")
    assert common.resolve_database_target(str(tmp_path)).sqlite_path.endswith("a.db")


def test_sqlite_target_resolves_the_file_the_url_actually_names(tmp_path, monkeypatch):
    """Confirming the backend is not enough — a non-default path must be used.

    `sqlite:///data/production.db` with a fixed `<local_dir>/silicon_notebook.db`
    reader still reports from a stale file.
    """
    common = load_common()
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/production.db")
    target = common.resolve_database_target(str(tmp_path))
    assert target.is_sqlite
    assert target.sqlite_path == str(tmp_path / "data" / "production.db")
    assert target.resolve_sqlite_file(str(tmp_path / "ignored")) == target.sqlite_path

    monkeypatch.setenv("DATABASE_URL", "sqlite:////abs/production.db")
    assert common.resolve_database_target(str(tmp_path)).sqlite_path == "/abs/production.db"

    # In-memory names no file, so the conventional location remains the fallback.
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    memory = common.resolve_database_target(str(tmp_path))
    assert memory.sqlite_path is None
    assert memory.resolve_sqlite_file("/tmp/x") == "/tmp/x/silicon_notebook.db"

    # A PostgreSQL deployment never yields a file to read.
    monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
    assert common.resolve_database_target(str(tmp_path)).resolve_sqlite_file("/tmp/x") is None


def test_database_target_matches_the_application_case_insensitivity(
    tmp_path, monkeypatch
):
    """`Settings(case_sensitive=False)` accepts `database_url=`, so this must too.

    Ignoring the lowercase spelling defaults to SQLite and puts the diagnostics
    back on a stale file while PostgreSQL is live.
    """
    common = load_common()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)

    (tmp_path / ".env").write_text("database_url=postgresql://h/db\n", encoding="utf-8")
    assert common.resolve_database_target(str(tmp_path)).backend == "postgres"

    (tmp_path / ".env").write_text("Database_Url=postgresql://h/db\n", encoding="utf-8")
    assert common.resolve_database_target(str(tmp_path)).backend == "postgres"

    # The process environment follows the same rule.
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("database_url", "postgresql://h/db")
    target = common.resolve_database_target(str(tmp_path))
    assert target.backend == "postgres" and target.source == "env"
    monkeypatch.delenv("database_url")

    # A different key that merely contains the name is still not a match.
    (tmp_path / ".env").write_text("MY_DATABASE_URL=postgresql://h/db\n", encoding="utf-8")
    assert common.resolve_database_target(str(tmp_path)).is_sqlite


def test_dotenv_values_drop_inline_comments_but_keep_quoted_hashes(
    tmp_path, monkeypatch
):
    """`DATABASE_URL=sqlite:///x.db # note` is valid dotenv; the comment is not path."""
    common = load_common()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)

    (tmp_path / ".env").write_text(
        "DATABASE_URL=sqlite:///.local/prod.db # production\n", encoding="utf-8"
    )
    target = common.resolve_database_target(str(tmp_path))
    assert target.sqlite_path == str(tmp_path / ".local" / "prod.db")

    (tmp_path / ".env").write_text(
        'DATABASE_URL="sqlite:///.local/prod.db" # production\n', encoding="utf-8"
    )
    assert common.resolve_database_target(str(tmp_path)).sqlite_path == str(
        tmp_path / ".local" / "prod.db"
    )

    # A `#` inside quotes belongs to the value.
    (tmp_path / ".env").write_text(
        'DATABASE_URL="postgresql://u:p#w@h/db"\n', encoding="utf-8"
    )
    assert common.resolve_database_target(str(tmp_path)).backend == "postgres"

    # A URL fragment with no preceding space is not a comment either.
    (tmp_path / ".env").write_text(
        "DATABASE_URL=sqlite:///.local/a#b.db\n", encoding="utf-8"
    )
    assert common.resolve_database_target(str(tmp_path)).sqlite_path.endswith("a#b.db")


def test_env_file_override_uses_the_exact_bootstrap_lookup(tmp_path, monkeypatch):
    """`app.core.config` reads this one with a plain `os.environ.get`.

    Honoring a lowercase spelling here would resolve a different backend than
    the service actually uses.
    """
    common = load_common()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://h/db\n", encoding="utf-8")
    ignored = tmp_path / "ignored.env"
    ignored.write_text("DATABASE_URL=sqlite:///./x.db\n", encoding="utf-8")

    monkeypatch.setenv("silicon_notebook_env_file", str(ignored))
    # The application ignores the lowercase spelling, so this must too.
    assert common.resolve_database_target(str(tmp_path)).backend == "postgres"
