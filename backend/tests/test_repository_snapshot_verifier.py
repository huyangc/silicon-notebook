"""Task 28: backup-only snapshot verifier for pre-refactor databases.

The verifier (``scripts/verify_repository_snapshot.py``) must prove that the
refactored repository can open a real, pre-refactor SQLite database WITHOUT
ever touching the original files: the original is only read via a
``mode=ro`` URI long enough for ``Connection.backup()``, the repository is
constructed exclusively on the temporary backup + a temporary storage
directory, and the only rows allowed to change on open are the documented
startup normalizations (interrupted-job recovery, seed inserts, the admin
in-place upgrade).  Its stdout may carry table names / counts / digests —
never row content.
"""
from __future__ import annotations

import importlib.util
import shutil
import socket
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
VERIFIER_PATH = ROOT / "scripts" / "verify_repository_snapshot.py"
FIXTURE_ROOT = ROOT / "backend" / "tests" / "fixtures" / "repository_v9"

# Strings seeded into the committed v9 fixture (see
# scripts/generate_repository_contract_fixtures.py) that must NEVER leak to
# the verifier's stdout/stderr: usernames, titles, sources, prompts, answers,
# report content and credentials/tokens.
FIXTURE_SECRETS = (
    "a00123456",
    "b00654321",
    "Fixture Owner",
    "Fixture amplifier notes",
    "Repository contract fixture",
    "Why is fixture gain stable?",
    "Why is gain stable?",
    "Source degeneration stabilizes gain",
    "source degeneration",
    "Explain gain stabilization",
    "Gain stabilization",
    "fixture-password",
    "session-fixture-token",
    "shr-fixture-token",
)


def _load_verifier():
    assert VERIFIER_PATH.is_file(), "scripts/verify_repository_snapshot.py is missing"
    spec = importlib.util.spec_from_file_location(
        "repository_snapshot_verifier", VERIFIER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses can resolve the module's
    # `from __future__ import annotations` string annotations (py3.13).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _copy_fixture(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "original.db"
    storage = tmp_path / "original-storage"
    shutil.copyfile(FIXTURE_ROOT / "baseline.db", database)
    shutil.copytree(FIXTURE_ROOT / "storage", storage)
    return database, storage


def _storage_stat(storage: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (
            str(path.relative_to(storage)),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in storage.rglob("*")
        if path.is_file()
    )


def _insert_running_jobs(database: Path) -> None:
    """Degrade the copy into a crashed-process shape: one running merge-review
    job plus one running ask job (cloned from the committed done job)."""
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO merge_review_jobs (notebook_id, status) "
            "VALUES ('nb-fixture', 'running')"
        )
        db.execute(
            "INSERT INTO ask_jobs "
            "SELECT 'askjob-running', notebook_id, conversation_id, created_by, "
            "mode, question, 'running', trace_json, answer_id, error, "
            "created_at, updated_at FROM ask_jobs WHERE id='askjob-fixture'"
        )
    db.close()


def _verify_with_repository_mutation(module, database, storage, mutation, monkeypatch):
    real_repository = module.SQLiteRepository

    class MutatingRepository(real_repository):
        def __init__(self, settings):
            super().__init__(settings)
            with self._write() as db:
                mutation(db)

    monkeypatch.setattr(module, "SQLiteRepository", MutatingRepository)
    return module.verify_snapshot(database, storage)


def test_repository_is_never_constructed_with_original_database_path(
    tmp_path, monkeypatch
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    captured: list = []
    real_repository = module.SQLiteRepository

    class RecordingRepository(real_repository):
        def __init__(self, settings):
            captured.append(settings)
            super().__init__(settings)

    real_connect = sqlite3.connect

    def recording_connect(target, *args, **kwargs):
        if str(database) in str(target):
            # The original may only ever be opened via a read-only URI.
            assert str(target).startswith("file:"), target
            assert "mode=ro" in str(target), target
            assert kwargs.get("uri") or (args and args[-1] is True)
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(module, "SQLiteRepository", RecordingRepository)
    monkeypatch.setattr(module.sqlite3, "connect", recording_connect)

    result = module.verify_snapshot(database, storage)
    assert result.ok, result.discrepancies
    assert captured, "verifier never constructed the repository"
    repo_db = Path(captured[0].sqlite_path).resolve()
    assert repo_db != database.resolve()
    assert database.resolve() not in repo_db.parents
    assert repo_db.is_absolute()


def test_repository_is_never_constructed_with_original_storage_path_or_symlink(
    tmp_path, monkeypatch
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    storage_link = tmp_path / "storage-link"
    storage_link.symlink_to(storage, target_is_directory=True)

    captured: list = []
    real_repository = module.SQLiteRepository

    class RecordingRepository(real_repository):
        def __init__(self, settings):
            captured.append(settings)
            super().__init__(settings)

    monkeypatch.setattr(module, "SQLiteRepository", RecordingRepository)
    result = module.verify_snapshot(database, storage_link)
    assert result.ok, result.discrepancies
    assert captured
    repo_storage = Path(captured[0].storage_dir)
    assert not repo_storage.is_symlink()
    resolved = repo_storage.resolve()
    original = storage.resolve()
    assert resolved != original
    assert original not in resolved.parents
    assert resolved != storage_link.resolve()


def test_wal_committed_rows_are_present_in_backup(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb-wal', 'wal notebook', 't0', 't0')"
        )
        writer.commit()
        wal = Path(f"{database}-wal")
        assert wal.is_file() and wal.stat().st_size > 0, (
            "test setup: committed row must still live in the WAL sidecar"
        )
        # Keep the writer connection open so the WAL cannot be checkpointed
        # away — the verifier's read-only backup must still see the row.
        result = module.verify_snapshot(database, storage)
    finally:
        writer.close()

    assert result.ok, result.discrepancies
    assert result.table_counts["notebooks"] == 2


def test_live_wal_shm_mtime_is_an_explicit_metadata_exception(tmp_path):
    import os

    module = _load_verifier()
    database, _storage = _copy_fixture(tmp_path)

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb-shm', 'shm exception', 't0', 't0')"
        )
        writer.commit()
        shm = Path(f"{database}-shm")
        assert shm.is_file(), "test setup: live WAL must have an SHM sidecar"
        before = module._source_metadata(database)
        stat = shm.stat()
        shm_size = stat.st_size
        os.utime(
            shm,
            ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
        )
        assert shm.stat().st_mtime_ns != stat.st_mtime_ns
        after = module._source_metadata(database)
    finally:
        writer.close()

    assert before == after
    assert before[-1] == (f"{database.name}-shm", shm_size, 0)


def test_live_wal_shm_size_change_fails_source_metadata_guard(tmp_path, monkeypatch):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb-shm-size', 'shm size guard', 't0', 't0')"
        )
        writer.commit()
        shm = Path(f"{database}-shm")
        assert shm.is_file(), "test setup: live WAL must have an SHM sidecar"
        initial_size = shm.stat().st_size
        real_exercise_reads = module.exercise_reads

        def resize_shm(repo, backup_path):
            counts = real_exercise_reads(repo, backup_path)
            with shm.open("ab") as stream:
                stream.write(b"size-change")
            assert shm.stat().st_size > initial_size
            return counts

        monkeypatch.setattr(module, "exercise_reads", resize_shm)
        result = module.verify_snapshot(database, storage)
    finally:
        writer.close()

    assert not result.ok
    assert result.source_unchanged is False
    assert "original-database-metadata-changed" in result.discrepancies


def test_schema_tables_counts_pks_and_digests_are_preserved(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 9
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []
    # Representative preserved counts from the committed fixture.
    assert result.table_counts["notebooks"] == 1
    assert result.table_counts["sources"] == 1
    assert result.table_counts["knowledge_objects"] == 2
    assert result.table_counts["knowledge_relations"] == 1
    # Every non-virtual table carries a digest; digests are opaque hex.
    assert result.table_digests["knowledge_objects"]
    assert all(
        set(d) <= set("0123456789abcdef") for d in result.table_digests.values() if d
    )
    # v9 -> v10 migration adds kg_rebuild_checkpoint; it must arrive empty and
    # be reported as a migration table, not a data change.
    assert "kg_rebuild_checkpoint" in result.migration_added_tables
    # Reads were exercised on the populated fixture.
    assert result.reads["notebooks"] == 1
    assert result.reads["sources"] == 1
    assert result.reads["knowledge_types"] >= 1
    assert result.reads["conversations"] >= 1
    assert result.reads["ask_jobs"] >= 1
    assert result.reads["reports"] >= 1


def test_deployed_v13_database_verifies_through_migration_14(tmp_path):
    """The (13, 14) hop is the one EVERY currently-deployed production
    database takes: v13 was the shipping schema before Task 1 added
    sources.memory_id. The v9-fixture tests above only exercise the (9, 14)
    manifest key, so a missing (13, 14) MIGRATION_MANIFEST entry would make
    the backup verifier fail closed (migration-manifest-missing +
    unmanifested column/index + schema-sql-changed) on every real upgrade
    while staying green in CI. Build the v13 shape by upgrading the fixture
    copy to current and rolling back exactly what _migration_14 adds."""
    from app.core.config import Settings

    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    # One real open migrates the copy 9 -> current and applies the startup
    # normalizations once, so the verification open below is a pure 13 -> 14
    # migration replay (scratch storage keeps the fixture storage pristine).
    upgraded = module.SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{database}",
            storage_dir=str(tmp_path / "upgrade-storage"),
        )
    )
    upgraded.close_local()
    rollback = sqlite3.connect(database)
    try:
        rollback.execute("DROP INDEX idx_sources_memory_id")
        rollback.execute("ALTER TABLE sources DROP COLUMN memory_id")
        rollback.execute("PRAGMA user_version = 13")
        rollback.commit()
    finally:
        rollback.close()

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 13
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


@pytest.mark.parametrize(
    "mutation, expected_fragment",
    [
        (
            lambda db: db.execute("CREATE TABLE verifier_backdoor (id TEXT)"),
            "verifier_backdoor",
        ),
        (
            lambda db: db.execute("ALTER TABLE notebooks ADD COLUMN verifier_flag TEXT"),
            "notebooks",
        ),
        (
            lambda db: (
                db.execute("DROP INDEX idx_chunks_nb"),
                db.execute("CREATE INDEX idx_chunks_nb ON chunks(source_id)"),
            ),
            "idx_chunks_nb",
        ),
        (
            lambda db: db.execute(
                "CREATE TRIGGER verifier_trigger AFTER INSERT ON notebooks "
                "BEGIN SELECT 1; END"
            ),
            "verifier_trigger",
        ),
        (
            lambda db: db.execute(
                "CREATE VIEW verifier_view AS SELECT id FROM notebooks"
            ),
            "verifier_view",
        ),
    ],
    ids=["empty-table", "column", "index-definition", "trigger", "view"],
)
def test_unmanifested_schema_changes_fail_verification(
    tmp_path, monkeypatch, mutation, expected_fragment
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    result = _verify_with_repository_mutation(
        module, database, storage, mutation, monkeypatch
    )

    assert not result.ok
    assert expected_fragment in " ".join(result.discrepancies)


def test_fake_builtin_seed_row_fails_verification(tmp_path, monkeypatch):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    result = _verify_with_repository_mutation(
        module,
        database,
        storage,
        lambda db: db.execute(
            "INSERT INTO concept_whitelist (term, note, created_at) "
            "VALUES ('verifier-backdoor', 'builtin', 'private-row-content')"
        ),
        monkeypatch,
    )

    assert not result.ok
    assert "concept_whitelist" in result.changed_tables


@pytest.mark.parametrize("filename", ["snapshot?.db", "snapshot#.db", "snapshot%.db"])
def test_database_uri_punctuation_is_percent_encoded(tmp_path, filename):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    punctuated = database.with_name(filename)
    database.rename(punctuated)

    result = module.verify_snapshot(punctuated, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 9


def test_cleanup_failure_is_authoritative_and_reports_only_retained_path(
    tmp_path, monkeypatch, capsys
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    retained: list[Path] = []

    def fail_cleanup(path, *args, **kwargs):
        retained.append(Path(path))
        raise OSError("private-row-content cleanup secret")

    monkeypatch.setattr(module.shutil, "rmtree", fail_cleanup)

    result = module.verify_snapshot(database, storage)
    module._print_report(result)
    output = capsys.readouterr().out

    assert not result.ok
    assert retained
    assert f"temporary_backup_retained={retained[0]}" in output
    assert "private-row-content" not in output


def test_primary_failure_is_preserved_when_cleanup_succeeds(tmp_path, monkeypatch):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    class ExplodingRepository:
        def __init__(self, _settings):
            raise RuntimeError("primary private row content")

    monkeypatch.setattr(module, "SQLiteRepository", ExplodingRepository)

    with pytest.raises(RuntimeError, match="primary private row content"):
        module.verify_snapshot(database, storage)


def test_cleanup_failure_wins_over_primary_failure_without_leaking_errors(
    tmp_path, monkeypatch
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    retained: list[Path] = []

    class ExplodingRepository:
        def __init__(self, _settings):
            raise RuntimeError("primary private row content")

    def fail_cleanup(path, *args, **kwargs):
        retained.append(Path(path))
        raise OSError("cleanup private row content")

    monkeypatch.setattr(module, "SQLiteRepository", ExplodingRepository)
    monkeypatch.setattr(module.shutil, "rmtree", fail_cleanup)

    with pytest.raises(SystemExit) as caught:
        module.verify_snapshot(database, storage)

    assert retained
    assert str(caught.value) == (
        "repository-snapshot: FAIL "
        f"temporary_backup_retained={retained[0]}"
    )
    assert "primary private row content" not in str(caught.value)
    assert "cleanup private row content" not in str(caught.value)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE knowledge_objects SET status='approved' WHERE id='ko-fixture-claim'",
        "DELETE FROM knowledge_relations WHERE id='rel-fixture'",
    ],
)
def test_row_mutation_or_deletion_after_open_fails_verification(
    tmp_path, monkeypatch, mutation
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    real_repository = module.SQLiteRepository

    class MutatingRepository(real_repository):
        def __init__(self, settings):
            super().__init__(settings)
            with self._write() as db:
                db.execute(mutation)

    monkeypatch.setattr(module, "SQLiteRepository", MutatingRepository)
    result = module.verify_snapshot(database, storage)
    assert not result.ok
    assert result.changed_tables


def test_only_recovery_seed_and_admin_upgrade_fields_are_normalized(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    _insert_running_jobs(database)
    with sqlite3.connect(database) as db:
        # Stale admin credentials: the seed's in-place upgrade rewrites them.
        db.execute(
            "UPDATE users SET password_hash='stale', password_salt='stale', "
            "password_iterations=1, updated_at='2020-01-01T00:00:00' "
            "WHERE id='user-local'"
        )
        # Drop one builtin seed row from each seeded table: reseed must
        # restore them without counting as a data change.
        db.execute(
            "DELETE FROM object_schemas WHERE source='builtin' AND object_type="
            "(SELECT object_type FROM object_schemas WHERE source='builtin' LIMIT 1)"
        )
        db.execute(
            "DELETE FROM concept_whitelist WHERE term="
            "(SELECT term FROM concept_whitelist LIMIT 1)"
        )
    db.close()

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.changed_tables == []
    assert result.normalized["merge_review_jobs"] == 1
    assert result.normalized["ask_jobs"] == 1
    assert result.normalized["admin_upgraded"] == 1
    assert result.normalized["seeded_object_schemas"] >= 1
    assert result.normalized["seeded_concept_whitelist"] >= 1


def test_normalization_never_excuses_other_job_field_changes(tmp_path, monkeypatch):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    _insert_running_jobs(database)
    real_repository = module.SQLiteRepository

    class TamperingRepository(real_repository):
        def __init__(self, settings):
            super().__init__(settings)
            with self._write() as db:
                # Same rows recovery touches, but a non-allowed column.
                db.execute(
                    "UPDATE ask_jobs SET mode='graph' WHERE id='askjob-running'"
                )

    monkeypatch.setattr(module, "SQLiteRepository", TamperingRepository)
    result = module.verify_snapshot(database, storage)
    assert not result.ok
    assert "ask_jobs" in result.changed_tables


def test_stdout_never_includes_seeded_secrets(tmp_path, capsys):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    _insert_running_jobs(database)

    exit_code = module.main(
        ["--database", str(database), "--storage-dir", str(storage)]
    )
    out = capsys.readouterr()
    assert exit_code == 0
    combined = out.out + out.err
    assert "repository-snapshot: PASS schema=v9 changed_tables=0" in out.out
    for secret in FIXTURE_SECRETS:
        assert secret not in combined, f"stdout leaked seeded content: {secret!r}"


def test_original_storage_list_size_mtime_remain_unchanged(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    before_storage = _storage_stat(storage)
    before_db = (database.stat().st_size, database.stat().st_mtime_ns)

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.storage_unchanged is True
    assert _storage_stat(storage) == before_storage
    assert (database.stat().st_size, database.stat().st_mtime_ns) == before_db
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_missing_database_or_storage_is_a_hard_failure(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    with pytest.raises(SystemExit):
        module.verify_snapshot(tmp_path / "absent.db", storage)
    with pytest.raises(SystemExit):
        module.verify_snapshot(database, tmp_path / "absent-storage")


def test_hostile_model_environment_creates_no_configured_client_and_no_network(
    tmp_path, monkeypatch, capsys
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    hostile_env = {
        "OPENAI_COMPAT_BASE_URL": "http://127.0.0.1:9/hostile",
        "OPENAI_COMPAT_API_KEY": "hostile-key",
        "OPENAI_COMPAT_MODEL": "hostile-model",
        "REASONING_LLM_BASE_URL": "http://127.0.0.1:9/hostile",
        "REASONING_LLM_API_KEY": "hostile-key",
        "REASONING_LLM_MODEL": "hostile-reasoning",
        "REWRITE_LLM_BASE_URL": "http://127.0.0.1:9/hostile",
        "REWRITE_LLM_API_KEY": "hostile-key",
        "REWRITE_LLM_MODEL": "hostile-rewrite",
        "KG_LLM_BASE_URL": "http://127.0.0.1:9/hostile",
        "KG_LLM_API_KEY": "hostile-key",
        "KG_LLM_MODEL": "hostile-kg",
        "EMBED_PROVIDER": "dashscope",
        "EMBED_BASE_URL": "http://127.0.0.1:9/hostile",
        "EMBED_API_KEY": "hostile-key",
        "EMBED_MODEL": "hostile-embed",
        "RERANK_MODEL": "hostile-rerank",
        "RERANK_BASE_URL": "http://127.0.0.1:9/hostile",
        "RERANK_API_KEY": "hostile-key",
        "MINERU_MODE": "http",
        "MINERU_API_URL": "http://127.0.0.1:9/hostile",
        "MINERU_API_TOKEN": "hostile-token",
        "SCALE_INDEX_AUTO_ENABLED": "true",
    }
    for key, value in hostile_env.items():
        monkeypatch.setenv(key, value)

    def no_network(*_args, **_kwargs):
        raise AssertionError("verifier attempted a network call")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)

    from app.core import llm as llm_module
    from app.services import embedding as embedding_module
    from app.services import mineru_client as mineru_module
    from app.services import mineru_cloud_client as mineru_cloud_module
    from app.services import rerank_client as rerank_module

    def guard_init(cls, label):
        real_init = cls.__init__

        def guarded(self, *args, **kwargs):
            real_init(self, *args, **kwargs)
            assert not self.configured, (
                f"hostile environment leaked into a configured {label} client"
            )

        return guarded

    monkeypatch.setattr(
        llm_module.OpenAICompatibleClient,
        "__init__",
        guard_init(llm_module.OpenAICompatibleClient, "LLM"),
    )
    monkeypatch.setattr(
        rerank_module.RerankClient,
        "__init__",
        guard_init(rerank_module.RerankClient, "rerank"),
    )
    monkeypatch.setattr(
        mineru_module.MinerUClient,
        "__init__",
        guard_init(mineru_module.MinerUClient, "MinerU"),
    )
    monkeypatch.setattr(
        mineru_cloud_module.MinerUCloudClient,
        "__init__",
        guard_init(mineru_cloud_module.MinerUCloudClient, "MinerU cloud"),
    )

    real_make_embedder = embedding_module.make_embedder

    def guarded_make_embedder(settings):
        embedder = real_make_embedder(settings)
        assert isinstance(embedder, embedding_module.FakeEmbedder), (
            "hostile environment leaked into a network embedder"
        )
        return embedder

    monkeypatch.setattr(embedding_module, "make_embedder", guarded_make_embedder)

    exit_code = module.main(
        ["--database", str(database), "--storage-dir", str(storage)]
    )
    out = capsys.readouterr()
    assert exit_code == 0
    assert "repository-snapshot: PASS schema=v9 changed_tables=0" in out.out
