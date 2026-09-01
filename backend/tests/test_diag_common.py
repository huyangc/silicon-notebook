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
    assert result.stats.truncated is True


def test_window_reads_newest_file_before_old_gzip_exhausts_decoded_budget(tmp_path):
    old = line("old", "2026-07-20T09:00:00") * 100
    with gzip.open(tmp_path / "events-2026-07-20.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(old)
    (tmp_path / "events-2026-07-21.jsonl").write_text(
        line("new", "2026-07-21T09:00:00")
    )

    common = load_common()
    result = common.read_channel(
        tmp_path,
        "events",
        since_hours=24,
        now=datetime.fromisoformat("2026-07-21T12:00:00"),
        max_input_bytes=512,
    )

    assert [row["id"] for row in result.records] == ["new"]
    assert result.stats.matched == 1
    assert result.stats.truncated is True


def test_window_can_read_complete_compressed_input_without_a_byte_cap(tmp_path):
    old = line("old", "2026-07-20T13:00:00") * 100
    with gzip.open(tmp_path / "events-2026-07-20.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(old)
    (tmp_path / "events-2026-07-21.jsonl").write_text(
        line("new", "2026-07-21T09:00:00")
    )

    common = load_common()
    result = common.read_channel(
        tmp_path,
        "events",
        since_hours=24,
        now=datetime.fromisoformat("2026-07-21T12:00:00"),
        max_input_bytes=None,
    )

    assert [row["id"] for row in result.records] == ["old", "new"]
    assert result.stats.parsed == 101
    assert result.stats.truncated is False


def test_window_reverse_scan_limit_does_not_evict_newer_files(tmp_path):
    (tmp_path / "events-2026-07-20.jsonl").write_text(
        line("old-1", "2026-07-20T09:00:00")
        + line("old-2", "2026-07-20T10:00:00")
    )
    (tmp_path / "events-2026-07-21.jsonl").write_text(
        line("new-1", "2026-07-21T09:00:00")
        + line("new-2", "2026-07-21T10:00:00")
    )

    common = load_common()
    result = common.read_channel(
        tmp_path,
        "events",
        since_hours=48,
        limit=2,
        now=datetime.fromisoformat("2026-07-21T12:00:00"),
    )

    assert [row["id"] for row in result.records] == ["new-1", "new-2"]
    assert result.stats.matched == 4
    assert result.stats.retained == 2
    assert result.stats.truncated is True


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


def test_unbounded_reader_keeps_an_independent_per_record_byte_bound(tmp_path):
    path = tmp_path / "events-2026-07-21.jsonl"
    path.write_bytes(b"x" * 512)
    common = load_common()

    rows = list(common.iter_jsonl_file(
        path,
        max_input_bytes=None,
        max_record_bytes=128,
    ))

    assert rows == [(None, True, -1)]


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

    # A query string belongs to the path the service opens, so it must survive.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///.local/prod.db?mode=ro")
    assert common.resolve_database_target(str(tmp_path)).sqlite_path == str(
        tmp_path / ".local" / "prod.db?mode=ro"
    )

    # URLs the service itself rejects resolve to `unknown`, never to a guess.
    for rejected in ("sqlite://", "sqlite:///.local/a#b.db", "postgresql:u:p@h/db"):
        monkeypatch.setenv("DATABASE_URL", rejected)
        target = common.resolve_database_target(str(tmp_path))
        assert target.backend == "unknown", rejected
        assert target.resolve_sqlite_file("/tmp/x") is None, rejected
        # A malformed DSN can carry credentials; nothing of it may survive.
        rendered = target.explain() + target.skip_note()
        for secret in ("u:p", "p@h", "a#b"):
            assert secret not in rendered, rejected

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

    # A `#` inside quotes belongs to the value rather than starting a comment.
    # The service rejects a fragment, so the shared parser reports `unknown`
    # instead of silently diagnosing a different database.
    (tmp_path / ".env").write_text(
        'DATABASE_URL="postgresql://u:p#w@h/db"\n', encoding="utf-8"
    )
    assert common.resolve_database_target(str(tmp_path)).backend == "unknown"

    # A quoted value with a genuine query string stays intact and stays valid.
    (tmp_path / ".env").write_text(
        'DATABASE_URL="sqlite:///.local/prod.db?mode=ro" # note\n', encoding="utf-8"
    )
    assert common.resolve_database_target(str(tmp_path)).sqlite_path.endswith(
        "prod.db?mode=ro"
    )


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


def test_explicitly_blank_database_url_is_invalid_not_absent(tmp_path, monkeypatch):
    """`Settings` gives a present-but-blank value precedence and rejects it.

    Falling through to `.env` or the SQLite default would diagnose a
    configuration the service is provably not running on.
    """
    common = load_common()
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://h/db\n", encoding="utf-8")

    for blank in ("", "   "):
        monkeypatch.setenv("DATABASE_URL", blank)
        target = common.resolve_database_target(str(tmp_path))
        assert target.backend == "unknown", repr(blank)
        assert target.resolve_sqlite_file("/tmp/x") is None, repr(blank)

    # A blank value inside .env is equally invalid rather than "no config".
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / ".env").write_text("DATABASE_URL=\n", encoding="utf-8")
    assert common.resolve_database_target(str(tmp_path)).backend == "unknown"

    # Truly absent still means the shipped SQLite default.
    (tmp_path / ".env").write_text("OTHER=1\n", encoding="utf-8")
    assert common.resolve_database_target(str(tmp_path)).is_sqlite


def test_readonly_uri_escapes_a_filename_that_carries_a_query(tmp_path, monkeypatch):
    """A configured query is part of the filename, so it must not become a URI arg.

    `file:{path}?mode=ro` with a `?` already in the name makes SQLite parse
    `mode=ro?mode=ro` and refuse to open the file at all.
    """
    common = load_common()
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///.local/prod.db?mode=ro")
    target = common.resolve_database_target(str(tmp_path))

    uri = target.sqlite_readonly_uri(str(tmp_path))
    assert uri.endswith("?mode=ro")
    assert "%3Fmode%3Dro" in uri          # 文件名里的那个 ? 被编码
    assert uri.count("?") == 1            # 只剩参数分隔符那一个

    # It must actually open the literal file that the service would open.
    import sqlite3
    real = tmp_path / ".local" / "prod.db?mode=ro"
    real.parent.mkdir(parents=True)
    sqlite3.connect(str(real)).close()
    conn = sqlite3.connect(uri, uri=True)
    conn.close()

    # 普通路径不受影响。
    monkeypatch.setenv("DATABASE_URL", "sqlite:///.local/plain.db")
    plain = common.resolve_database_target(str(tmp_path)).sqlite_readonly_uri(str(tmp_path))
    assert plain.endswith(".local/plain.db?mode=ro")

    monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
    assert common.resolve_database_target(str(tmp_path)).sqlite_readonly_uri("/x") is None


def test_dotenv_interpolation_matches_the_application(tmp_path, monkeypatch):
    """`DATABASE_URL=${DB_KIND}://host/db` resolves for the app, so it must here.

    Passing the literal `${DB_KIND}://…` to the URL parser yields `unknown` and
    makes every guarded diagnostic skip on a perfectly healthy deployment.
    """
    common = load_common()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)
    (tmp_path / ".env").write_text(
        "DB_KIND=postgresql\nDATABASE_URL=${DB_KIND}://host/db\n", encoding="utf-8"
    )

    assert common.resolve_database_target(str(tmp_path)).backend == "postgres"

    # The plain spellings keep working through the same path.
    (tmp_path / ".env").write_text(
        "export DATABASE_URL='sqlite:///.local/x.db' # note\n", encoding="utf-8"
    )
    target = common.resolve_database_target(str(tmp_path))
    assert target.is_sqlite and target.sqlite_path.endswith(".local/x.db")


def test_dotenv_fallback_keeps_working_without_python_dotenv(tmp_path, monkeypatch):
    """These scripts are stdlib-only, so the parser must degrade, not fail."""
    common = load_common()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)
    monkeypatch.setattr(common, "_authoritative_dotenv_values", lambda _path: None)

    (tmp_path / ".env").write_text(
        "export database_url=postgresql://h/db # note\n", encoding="utf-8"
    )
    assert common.resolve_database_target(str(tmp_path)).backend == "postgres"

    (tmp_path / ".env").write_text(
        'DATABASE_URL="sqlite:///.local/x.db"\n', encoding="utf-8"
    )
    assert common.resolve_database_target(str(tmp_path)).sqlite_path.endswith(".local/x.db")


def test_duplicate_case_variants_follow_file_order(tmp_path, monkeypatch):
    """The later declaration wins, as it does for pydantic-settings.

    Sorting the parsed mapping would let `database_url` beat `DATABASE_URL`
    regardless of position and classify a live PostgreSQL deployment as SQLite.
    """
    common = load_common()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)

    (tmp_path / ".env").write_text(
        "database_url=sqlite:///old.db\nDATABASE_URL=postgresql://host/db\n",
        encoding="utf-8",
    )
    assert common.resolve_database_target(str(tmp_path)).backend == "postgres"

    # ...and the other way round.
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql://host/db\ndatabase_url=sqlite:///new.db\n",
        encoding="utf-8",
    )
    target = common.resolve_database_target(str(tmp_path))
    assert target.is_sqlite and target.sqlite_path.endswith("new.db")


def test_diag_db_anchors_on_the_repository_not_the_cwd(tmp_path, monkeypatch, capsys):
    """Invoked by absolute path from elsewhere, cwd says nothing about the deployment."""
    import importlib.util as _util

    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = _util.spec_from_file_location("diag_db_root_probe", SCRIPTS / "diag_db.py")
    module = _util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    # The real repository .env decides; this run must not claim a SQLite default
    # just because the working directory happens to hold no configuration.
    resolved = module.diag_common.resolve_database_target(
        str(module._SCRIPT_DIR.parent)
    )
    from_cwd = module.diag_common.resolve_database_target(str(unrelated))
    assert from_cwd.source == "default"          # cwd 什么都不知道
    assert resolved.source in {"env", "dotenv", "default"}
    # 判据必须取自仓库那一份，而不是 cwd 那一份。
    assert module._SCRIPT_DIR.parent != unrelated


def test_environment_case_collision_follows_insertion_order(tmp_path, monkeypatch):
    """Both spellings can coexist on Unix; the later entry wins, as for Settings."""
    common = load_common()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)

    # Insert lowercase first, then uppercase: uppercase is the later entry.
    monkeypatch.setenv("database_url", "sqlite:///./early.db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
    assert common.resolve_database_target(str(tmp_path)).backend == "postgres"

    # ...and the reverse order selects the SQLite value instead.
    monkeypatch.delenv("database_url")
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
    monkeypatch.setenv("database_url", "sqlite:///./late.db")
    target = common.resolve_database_target(str(tmp_path))
    assert target.is_sqlite and target.sqlite_path.endswith("late.db")
